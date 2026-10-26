;;; org-roam-retrieval-ui.el --- Retrieval result panes -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later
;; Package-Requires: ((emacs "29.1"))

;;; Commentary:
;; One read-only interaction model for search and related-note results.
;; Sessions own source markers and async request identities.  Results arriving
;; after close or replacement cannot reopen a pane or modify source text.

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'org)
(require 'org-id)
(require 'org-fold)
(require 'subr-x)

(defvar nov-documents)
(defvar nov-work-dir)
(declare-function nov-goto-document "nov" (index))
(declare-function nov-visit-relative-file "nov" (filename target))
(declare-function pdf-view-mode "pdf-view" ())

(declare-function org-roam-retrieval-client-cancel "org-roam-retrieval-client" (id))
(declare-function org-roam-retrieval-status "org-roam-retrieval" ())

(cl-defstruct (org-roam-retrieval-session
               (:constructor org-roam-retrieval-session-create))
  id buffer source marker tick query scope root-label request-id last-rpc-id closed
  results response refresh append-p)

(defvar-local org-roam-retrieval-ui--session nil)

(defvar-keymap org-roam-retrieval-results-mode-map
  :doc "Keys for Org-roam retrieval results."
  "RET" #'org-roam-retrieval-ui-open
  "n" #'org-roam-retrieval-ui-next
  "p" #'org-roam-retrieval-ui-previous
  "i" #'org-roam-retrieval-ui-insert
  "g" #'org-roam-retrieval-ui-refresh
  "q" #'org-roam-retrieval-ui-quit
  "s" #'org-roam-retrieval-status
  "C-c C-k" #'org-roam-retrieval-ui-cancel)

(define-derived-mode org-roam-retrieval-results-mode special-mode "Roam Retrieval"
  "Read-only related notes and library passages.
RET opens a result, i inserts its link, n/p move, g refreshes, q closes."
  (setq-local mouse-highlight nil)
  (variable-pitch-mode 1)
  (display-line-numbers-mode -1))

(defun org-roam-retrieval-ui--plain (text)
  "Return TEXT as a safe single line of preview prose."
  (string-trim (replace-regexp-in-string "[[:space:]\n\r]+" " " (or text ""))))

(defun org-roam-retrieval-ui--current-result ()
  "Return the result represented by the line at point."
  (or (get-text-property (point) 'org-roam-retrieval-result)
      (get-text-property (line-beginning-position) 'org-roam-retrieval-result)))

(defun org-roam-retrieval-ui--live-p (session)
  "Whether SESSION still owns an open result buffer."
  (and (not (org-roam-retrieval-session-closed session))
       (buffer-live-p (org-roam-retrieval-session-buffer session))
       (eq session (buffer-local-value
                    'org-roam-retrieval-ui--session
                    (org-roam-retrieval-session-buffer session)))))

(defun org-roam-retrieval-ui--cancel-session (session)
  "Cancel lexical or deferred semantic work belonging to SESSION."
  (let ((id (or (org-roam-retrieval-session-request-id session)
                (org-roam-retrieval-session-last-rpc-id session))))
    (setf (org-roam-retrieval-session-id session) (gensym "cancelled-")
          (org-roam-retrieval-session-request-id session) nil
          (org-roam-retrieval-session-last-rpc-id session) nil)
    (when id (org-roam-retrieval-client-cancel id))))

(defun org-roam-retrieval-ui--kill ()
  "Release the session owned by a killed preview buffer."
  (when org-roam-retrieval-ui--session
    (setf (org-roam-retrieval-session-closed org-roam-retrieval-ui--session) t)
    (org-roam-retrieval-ui--cancel-session org-roam-retrieval-ui--session)
    (when-let* ((marker (org-roam-retrieval-session-marker org-roam-retrieval-ui--session)))
      (set-marker marker nil))))

(defun org-roam-retrieval-ui-create (source marker query scope root-label append-p)
  "Create a session for SOURCE and MARKER using QUERY, SCOPE and ROOT-LABEL.
APPEND-P requests whole-list insertion only while the source is unchanged."
  (let* ((name (format (if append-p "*Org-roam insertion: %s*"
                         "*Org-roam retrieval: %s*") (buffer-name source)))
         (buffer (get-buffer-create name))
         (session (org-roam-retrieval-session-create
                   :id (gensym "retrieval-") :buffer buffer :source source
                   :marker marker :tick (with-current-buffer source (buffer-chars-modified-tick))
                   :query query :scope scope :root-label root-label :append-p append-p)))
    (with-current-buffer buffer
      (when org-roam-retrieval-ui--session
        (org-roam-retrieval-ui--kill))
      (org-roam-retrieval-results-mode)
      (setq-local org-roam-retrieval-ui--session session)
      (setq-local truncate-lines nil)
      (visual-line-mode 1)
      (add-hook 'kill-buffer-hook #'org-roam-retrieval-ui--kill nil t))
    (org-roam-retrieval-ui-state session "Searching…" (not append-p))
    session))

(defun org-roam-retrieval-ui--location-label (result)
  "Describe the RESULT's typed source location."
  (let* ((locator (alist-get 'locator result))
         (kind (alist-get 'kind locator)))
    (pcase kind
      ("pdf-page" (format "PDF · page %s" (alist-get 'page locator)))
      ("sheet-range" (format "%s · %s" (or (alist-get 'sheet locator) "Sheet")
                              (or (alist-get 'range locator) "")))
      ((or "org-node" "org-section")
       (if-let* ((outline (alist-get 'outline locator)))
           (string-join (append outline nil) " › ") "Org note"))
      ("text-range" (format "Text · line %s" (or (alist-get 'line locator) 1)))
      ("epub-section" (concat "EPUB · " (or (alist-get 'href locator) "section")))
      (_ "File"))))

(defun org-roam-retrieval-ui--render (session state)
  "Render SESSION with human-readable STATE, preserving selected identity."
  (when (org-roam-retrieval-ui--live-p session)
    (with-current-buffer (org-roam-retrieval-session-buffer session)
      (let* ((selected (alist-get 'id (org-roam-retrieval-ui--current-result)))
             (inhibit-read-only t)
             (source (org-roam-retrieval-session-source session))
             target first)
        (erase-buffer)
        (insert (propertize "Org-roam retrieval\n" 'face 'org-document-title))
        (insert (propertize "RET open  i insert link  n/p move  g refresh  s status  q close\n\n"
                            'face 'shadow))
        (insert "Scope: " (org-roam-retrieval-session-scope session)
                " · " (or (org-roam-retrieval-session-root-label session) "all sources") "\n")
        (when (buffer-live-p source)
          (insert "From: " (org-roam-retrieval-ui--plain (buffer-name source)) "\n"))
        (insert (propertize (concat state "\n\n") 'face 'font-lock-comment-face))
        (if (org-roam-retrieval-session-results session)
            (cl-loop for result in (org-roam-retrieval-session-results session)
                     for number from 1 do
                     (let ((start (point)))
                       (setq first (or first start))
                       (when (equal selected (alist-get 'id result)) (setq target start))
                       (insert (propertize (format "%d. %s\n" number
                                                   (org-roam-retrieval-ui--plain (alist-get 'title result)))
                                           'face 'org-level-2))
                       (insert (propertize (concat (org-roam-retrieval-ui--location-label result) "\n")
                                           'face '(:inherit shadow :height 0.9)))
                       (insert (propertize (concat (abbreviate-file-name (alist-get 'path result)) "\n")
                                           'face '(:inherit (shadow fixed-pitch) :height 0.8)))
                       (insert (org-roam-retrieval-ui--plain (alist-get 'excerpt result)) "\n\n")
                       (add-text-properties start (point)
                                            (list 'org-roam-retrieval-result result
                                                  'help-echo "RET opens this location; i inserts its link"))))
          (unless (string-match-p "[Ss]earching\\|[Ii]ndexing" state)
            (insert "Try a distinctive term, expand the source scope, or press s to check index coverage.\n")))
        (goto-char (or target first (point-min)))
        (set-buffer-modified-p nil)))))

(defun org-roam-retrieval-ui-state (session state &optional display)
  "Show STATE for SESSION, preserving any prior results.
DISPLAY is used only by an explicit user action, never by late callbacks."
  (org-roam-retrieval-ui--render session state)
  (when (and display (org-roam-retrieval-ui--live-p session))
    (save-current-buffer
      (let* ((buffer (org-roam-retrieval-session-buffer session))
             (main (if (not (window-parameter (selected-window) 'window-side))
                       (selected-window)
                     (cl-find-if (lambda (window)
                                   (not (window-parameter window 'window-side)))
                                 (window-list)))))
        (dolist (window (get-buffer-window-list buffer nil (selected-frame)))
          (when (window-parameter window 'window-side)
            (quit-window nil window)))
        (when (window-live-p main) (select-window main))
        (pop-to-buffer-same-window buffer)))))

(defun org-roam-retrieval-ui-update (session response)
  "Apply RESPONSE to SESSION without stealing focus or reopening its window."
  (when (org-roam-retrieval-ui--live-p session)
    (setf (org-roam-retrieval-session-request-id session) nil
          (org-roam-retrieval-session-response session) response
          (org-roam-retrieval-session-results session) (append (alist-get 'results response) nil))
    (let* ((count (length (org-roam-retrieval-session-results session)))
           (degraded (alist-get 'degraded response))
           (coverage (alist-get 'coverage response))
           (pending (eq t (alist-get 'semantic_pending response)))
           (indexing (or (eq t (alist-get 'indexing response))
                         (and (numberp (alist-get 'backlog response))
                              (> (alist-get 'backlog response) 0)))))
      (org-roam-retrieval-ui-state
       session
       (concat
        (cond
         ((and (zerop count) indexing) "Indexing sources; results will update automatically.")
         ((and (zerop count) pending) "Semantic search warming; results will update automatically.")
         ((zerop count) "No matches.")
         (t (format "%d result%s." count (if (= count 1) "" "s"))))
        (when (and indexing (> count 0)) " Indexing continues; more matches may appear.")
        (when pending " Showing lexical matches while semantic search completes.")
        (when (and degraded (not (eq degraded :false))) (concat " " (format "%s" degraded)))
        (when coverage
          (format " Coverage: %s sources, %s/%s eligible passages embedded."
                  (or (alist-get 'sources coverage) 0)
                  (or (alist-get 'embedded_chunks coverage) 0)
                  (or (alist-get 'eligible_chunks coverage)
                      (alist-get 'chunks coverage) 0))))))))

(defun org-roam-retrieval-ui-result-link (result)
  "Return a durable, location-bearing Org link for RESULT."
  (let* ((locator (alist-get 'locator result))
         (path (alist-get 'path result))
         (id (and (equal (alist-get 'kind locator) "org-node") (alist-get 'id locator)))
         (title (replace-regexp-in-string "[][]" " "
                                         (org-roam-retrieval-ui--plain (alist-get 'title result))))
         (encoded-locator (copy-tree locator))
         (_ (when (assq 'outline encoded-locator)
              (setf (alist-get 'outline encoded-locator)
                    (vconcat (alist-get 'outline encoded-locator)))))
         (link (cond
                (id (org-id-add-location id path) (concat "id:" id))
                ((equal (alist-get 'kind locator) "pdf-page")
                 (format "pdf:%s::%d" (org-link-escape path)
                         (max 1 (or (alist-get 'page locator) 1))))
                (t (concat "roam-source:"
                           (base64-encode-string
                            (json-serialize `((path . ,path) (locator . ,encoded-locator))) t))))))
    (org-link-make-string link title)))

(defun org-roam-retrieval-ui-open-location (path locator &optional other-window)
  "Open PATH at LOCATOR.  Use OTHER-WINDOW for interactive result navigation."
  (unless (and (stringp path) (not (file-remote-p path)) (file-exists-p path))
    (user-error "Source file is missing or is not local: %s" path))
  (when (equal (alist-get 'kind locator) "pdf-page")
    (require 'pdf-tools nil t))
  (let ((buffer (find-file-noselect path)))
    (if other-window (pop-to-buffer buffer) (switch-to-buffer buffer))
    (pcase (alist-get 'kind locator)
      ("org-node"
       (widen)
       (let ((id (alist-get 'id locator)))
         (unless (and id
                      (save-restriction
                        (widen)
                        (goto-char (point-min))
                        (re-search-forward (concat "^[ \t]*:ID:[ \t]+" (regexp-quote id) "[ \t]*$") nil t)))
           (user-error "This node moved or changed; refresh the retrieval index"))
         (if (org-before-first-heading-p) (goto-char (point-min)) (org-back-to-heading t))
         (org-fold-show-context 'link-search)))
      ("org-section"
       (widen)
       (let* ((outline (append (alist-get 'outline locator) nil))
              (position (if outline (org-find-olp outline t) (point-min))))
         (unless position
           (user-error "This heading moved or changed; refresh the retrieval index"))
         (goto-char position)
         (org-fold-show-context 'link-search)))
      ("pdf-page"
       (unless (or (derived-mode-p 'pdf-view-mode) (derived-mode-p 'doc-view-mode))
         (when (require 'pdf-tools nil t) (pdf-view-mode)))
       (let ((page (alist-get 'page locator)))
         (cond ((and (derived-mode-p 'pdf-view-mode) (fboundp 'pdf-view-goto-page))
                (pdf-view-goto-page page))
               ((and (derived-mode-p 'doc-view-mode) (fboundp 'doc-view-goto-page))
                (doc-view-goto-page page))
               (t (message "Requested PDF page %s; this viewer has no page navigation" page)))))
      ("text-range"
       (goto-char (point-min))
       (forward-line (1- (max 1 (or (alist-get 'line locator) 1)))))
      ("epub-section"
       (if (and (derived-mode-p 'nov-mode) (boundp 'nov-documents)
                (boundp 'nov-work-dir) (fboundp 'nov-goto-document))
           (let* ((href (alist-get 'href locator))
                  (target (and href (expand-file-name href nov-work-dir)))
                  (index (and target
                              (cl-position-if
                               (lambda (document)
                                 (equal (expand-file-name (cdr document)) target))
                               nov-documents))))
             (unless index (user-error "This EPUB section changed; reconcile its source"))
             (nov-goto-document index)
             (when-let* ((fragment (alist-get 'fragment locator)))
               (nov-visit-relative-file "" fragment)))
         (message "EPUB section %s; this reader has no section navigation"
                  (alist-get 'href locator))))
      ("sheet-range"
       (message "Source sheet %s, range %s"
                (alist-get 'sheet locator) (alist-get 'range locator))))))

(defun org-roam-retrieval-ui-follow-source (encoded _argument)
  "Follow a location-bearing link encoded in ENCODED."
  (let ((data (condition-case nil
                  (json-parse-string (base64-decode-string encoded) :object-type 'alist)
                (error (user-error "Invalid Roam source link")))))
    (org-roam-retrieval-ui-open-location (alist-get 'path data) (alist-get 'locator data))))

(org-link-set-parameters "roam-source" :follow #'org-roam-retrieval-ui-follow-source)

(defun org-roam-retrieval-ui-follow-pdf (target _argument)
  "Open a readable PDF TARGET of the form path::page."
  (let ((page 1) (path target))
    (when (string-match "\\`\\(.*\\)::\\([1-9][0-9]*\\)\\'" target)
      (setq path (match-string 1 target)
            page (string-to-number (match-string 2 target))))
    (org-roam-retrieval-ui-open-location
     (org-link-unescape path) `((kind . "pdf-page") (page . ,page)))))

(org-link-set-parameters "pdf" :follow #'org-roam-retrieval-ui-follow-pdf)


(defun org-roam-retrieval-ui-open ()
  "Open the result at point at its exact source location."
  (interactive nil org-roam-retrieval-results-mode)
  (let ((result (org-roam-retrieval-ui--current-result)))
    (unless result (user-error "Move to a result first with n or p"))
    (org-roam-retrieval-ui-open-location (alist-get 'path result) (alist-get 'locator result) t)))

(defun org-roam-retrieval-ui-insert ()
  "Insert the selected result's link at the remembered source cursor."
  (interactive nil org-roam-retrieval-results-mode)
  (let* ((session org-roam-retrieval-ui--session)
         (result (org-roam-retrieval-ui--current-result))
         (marker (and session (org-roam-retrieval-session-marker session))))
    (unless result (user-error "Move to a result first with n or p"))
    (unless (and (markerp marker) (marker-buffer marker)) (user-error "The source buffer was closed"))
    (with-current-buffer (marker-buffer marker)
      (barf-if-buffer-read-only)
      (save-excursion
        (goto-char marker)
        (atomic-change-group (insert (org-roam-retrieval-ui-result-link result)))))
    (message "Inserted link in %s" (buffer-name (marker-buffer marker)))))

(defun org-roam-retrieval-ui-next ()
  "Move to the next result, wrapping at the end."
  (interactive nil org-roam-retrieval-results-mode)
  (let* ((session org-roam-retrieval-ui--session)
         (current (org-roam-retrieval-ui--current-result))
         (results (and session (org-roam-retrieval-session-results session)))
         (next (or (cadr (member current results)) (car results))))
    (unless next (user-error "There are no results yet"))
    (goto-char (point-min))
    (while (and (< (point) (point-max))
                (not (equal next (org-roam-retrieval-ui--current-result))))
      (forward-line 1))))

(defun org-roam-retrieval-ui-previous ()
  "Move to the preceding result, wrapping at the start."
  (interactive nil org-roam-retrieval-results-mode)
  (let* ((session org-roam-retrieval-ui--session)
         (current (org-roam-retrieval-ui--current-result))
         (results (reverse (and session (copy-sequence (org-roam-retrieval-session-results session)))))
         (previous (or (cadr (member current results)) (car results))))
    (unless previous (user-error "There are no results yet"))
    (goto-char (point-min))
    (while (and (< (point) (point-max))
                (not (equal previous (org-roam-retrieval-ui--current-result))))
      (forward-line 1))))

(defun org-roam-retrieval-ui-refresh ()
  "Refresh this session's query without changing its source scope."
  (interactive nil org-roam-retrieval-results-mode)
  (unless (and org-roam-retrieval-ui--session
               (functionp (org-roam-retrieval-session-refresh org-roam-retrieval-ui--session)))
    (user-error "No query to refresh"))
  (funcall (org-roam-retrieval-session-refresh org-roam-retrieval-ui--session)))

(defun org-roam-retrieval-ui-cancel ()
  "Cancel pending work while retaining the current results."
  (interactive nil org-roam-retrieval-results-mode)
  (when org-roam-retrieval-ui--session
    (org-roam-retrieval-ui--cancel-session org-roam-retrieval-ui--session)
    (org-roam-retrieval-ui-state org-roam-retrieval-ui--session "Cancelled. Press g to retry.")
    (setf (org-roam-retrieval-session-closed org-roam-retrieval-ui--session) t)))

(defun org-roam-retrieval-ui-quit ()
  "Close this result pane and reject all late responses."
  (interactive nil org-roam-retrieval-results-mode)
  (when org-roam-retrieval-ui--session
    (setf (org-roam-retrieval-session-closed org-roam-retrieval-ui--session) t)
    (org-roam-retrieval-ui--cancel-session org-roam-retrieval-ui--session))
  (when-let* ((window (get-buffer-window (current-buffer) t)))
    (quit-window nil window)))

(provide 'org-roam-retrieval-ui)
;;; org-roam-retrieval-ui.el ends here
