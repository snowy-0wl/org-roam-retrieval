;;; org-roam-retrieval.el --- Integrated library and related-note retrieval -*- lexical-binding: t; -*-

;; SPDX-License-Identifier: GPL-3.0-or-later
;; Package-Requires: ((emacs "29.1") (org-roam "2.2"))

;;; Commentary:
;; Org-roam owns the interaction; a managed external worker owns extraction,
;; embeddings and derived indexes.  The compatibility commands at the end of
;; this library do not load Vdeft or org-similarity.

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'org)
(require 'org-roam-retrieval-client)
(require 'org-roam-retrieval-extract)
(require 'org-roam-retrieval-ui)
(require 'subr-x)

(defcustom org-roam-retrieval-result-limit 12
  "Maximum grouped results shown by a search."
  :type 'natnum :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-related-root-ids '("org-roam")
  "Root IDs searched for related notes without a prefix argument.
Nil searches every root.  Library search always searches all configured roots."
  :type '(repeat string) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-insert-heading "* Related notes"
  "Heading used when appending a complete list of related notes."
  :type 'string :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-search-mode "hybrid"
  "Ranking mode requested by default.  Semantic failure degrades to lexical."
  :type '(choice (const "hybrid") (const "lexical")) :group 'org-roam-retrieval)

(defvar org-roam-retrieval--sessions nil
  "Live query sessions; source buffers are never silently saved.")

(defun org-roam-retrieval--nearest-node ()
  "Return (ID POSITION END TITLE) for the nearest authored Org node.
Use the buffer rather than the graph database, preserving unsaved edits."
  (when (derived-mode-p 'org-mode)
    (save-restriction
      (widen)
      (save-excursion
        (unless (org-before-first-heading-p) (org-back-to-heading t))
        (let (found)
          (while (not found)
            (if-let* ((id (org-entry-get nil "ID")))
                (setq found
                      (list id (point)
                            (if (org-before-first-heading-p) (point-max)
                              (save-excursion (org-end-of-subtree t t) (point)))
                            (if (org-before-first-heading-p) (buffer-name)
                              (org-get-heading t t t t))))
              (if (org-up-heading-safe) nil
                (if (> (point) (point-min)) (goto-char (point-min))
                  (setq found 'none)))))
          (unless (eq found 'none) found))))))

(defun org-roam-retrieval--snapshot ()
  "Return a plain-text source snapshot and its exact search scope."
  (let* ((node (org-roam-retrieval--nearest-node))
         (region (use-region-p))
         (narrowed (buffer-narrowed-p))
         (scope (cond (region "region") (narrowed "region") (node "node") (t "file")))
         (text (cond
                (region (buffer-substring-no-properties (region-beginning) (region-end)))
                (narrowed (buffer-substring-no-properties (point-min) (point-max)))
                (node (save-restriction
                        (widen)
                        (buffer-substring-no-properties (nth 1 node) (nth 2 node))))
                (t (buffer-substring-no-properties (point-min) (point-max)))))
         (path (when (and buffer-file-name (not (file-remote-p buffer-file-name)))
                 (file-truename buffer-file-name))))
    `((text . ,(if (derived-mode-p 'org-mode) (org-roam-retrieval-extract-text text) text)) (scope . ,scope)
      (label . ,(pcase scope ("node" (concat "node: " (or (nth 3 node) "untitled")))
                       ("region" (if region "selected region" "narrowed buffer")) (_ "whole buffer")))
      (source . ((path . ,path) (node_id . ,(car node)))))))

(defun org-roam-retrieval--valid-related-roots (all-roots)
  "Return configured note root IDs unless ALL-ROOTS requests the library."
  (unless all-roots
    (let ((available (mapcar (lambda (root) (alist-get 'id root)) org-roam-retrieval-roots)))
      (let ((roots (cl-intersection org-roam-retrieval-related-root-ids available :test #'equal)))
        (when (and org-roam-retrieval-related-root-ids (not roots))
          (user-error "No related-note root is configured; set org-roam-retrieval-related-root-ids or use a prefix for all sources"))
        roots))))

(defun org-roam-retrieval--append (session results)
  "Append RESULTS only while SESSION's source still matches its snapshot.
Return non-nil if insertion happened."
  (let ((source (org-roam-retrieval-session-source session)))
    (when (and results (buffer-live-p source)
               (with-current-buffer source
                 (and (derived-mode-p 'org-mode) (not buffer-read-only)
                      (= (buffer-chars-modified-tick) (org-roam-retrieval-session-tick session)))))
      (with-current-buffer source
        (save-excursion
          (goto-char (point-max))
          (atomic-change-group
            (insert "\n" org-roam-retrieval-insert-heading "\n")
            (dolist (result results)
              (insert "- " (org-roam-retrieval-ui-result-link result) "\n")))))
      t)))

(defun org-roam-retrieval--deliver (session identity response)
  "Apply RESPONSE to SESSION only while its submission IDENTITY is current."
  (when (and (eq identity (org-roam-retrieval-session-id session))
             (org-roam-retrieval-ui--live-p session))
    (org-roam-retrieval-ui-update session response)
    (when (org-roam-retrieval-session-append-p session)
      (let ((pending (or (eq t (alist-get 'semantic_pending response))
                         (eq t (alist-get 'indexing response)))))
        (unless pending
          (let ((state (if (org-roam-retrieval--append session (append (alist-get 'results response) nil))
                           "Related-note links inserted."
                         "No links inserted: source changed, is read-only, was closed, or no matches were found.")))
            (org-roam-retrieval-ui-state session state)
            (message "%s" state))
          (setf (org-roam-retrieval-session-append-p session) nil))))))

(defun org-roam-retrieval--failure-state (session state)
  "Report STATE visibly for a query or in the echo area for quiet insertion."
  (org-roam-retrieval-ui-state session state)
  (when (org-roam-retrieval-session-append-p session) (message "%s" state)))

(defun org-roam-retrieval--submit (session params)
  "Submit PARAMS for SESSION, ignoring callbacks from replaced requests."
  (org-roam-retrieval-ui--cancel-session session)
  (setf (org-roam-retrieval-session-closed session) nil)
  (unless (org-roam-retrieval-session-response session)
    (org-roam-retrieval-ui-state session "Searching…"))
  (let ((identity (gensym "request-")))
    (setf (org-roam-retrieval-session-id session) identity)
    (condition-case err
        (let ((id
               (org-roam-retrieval-client-request
                "search" params
                (lambda (response) (org-roam-retrieval--deliver session identity response))
                (lambda (failure)
                  (when (and (eq identity (org-roam-retrieval-session-id session))
                             (org-roam-retrieval-ui--live-p session))
                    (setf (org-roam-retrieval-session-request-id session) nil)
                    (org-roam-retrieval--failure-state
                     session (format "Search unavailable: %s. Press g to retry or s for status."
                                     (or (alist-get 'message failure) failure))))))))
          (setf (org-roam-retrieval-session-request-id session) id
                (org-roam-retrieval-session-last-rpc-id session) id))
      (error
       (org-roam-retrieval--failure-state
        session (format "Search unavailable: %s. Press s for setup or repair." (error-message-string err)))))))

(defvar org-roam-retrieval--refresh-timer nil)
(defvar org-roam-retrieval--coverage-key nil)
(defvar org-roam-retrieval--semantic-ready-p nil)
(defvar org-roam-retrieval--refresh-lexical-p nil)

(defun org-roam-retrieval--refresh-visible ()
  "Refresh visible queries and pending quiet insertions after coverage changes."
  (setq org-roam-retrieval--refresh-timer nil)
  (let ((mode (when org-roam-retrieval--refresh-lexical-p "lexical")))
    (dolist (session org-roam-retrieval--sessions)
      (when (and (org-roam-retrieval-ui--live-p session)
                 (buffer-live-p (org-roam-retrieval-session-source session))
                 (or (org-roam-retrieval-session-append-p session)
                     (get-buffer-window (org-roam-retrieval-session-buffer session) t))
                 (functionp (org-roam-retrieval-session-refresh session)))
        (funcall (org-roam-retrieval-session-refresh session) mode)))))

(defun org-roam-retrieval--worker-event (method params)
  "Apply worker notification METHOD and PARAMS to current result sessions."
  (cond
   ((equal method "search-update")
    (let ((request (alist-get 'request_id params)))
      (dolist (session org-roam-retrieval--sessions)
        (when (and request (equal request (org-roam-retrieval-session-last-rpc-id session))
                   (org-roam-retrieval-ui--live-p session))
          (org-roam-retrieval--deliver session (org-roam-retrieval-session-id session)
                                       (alist-get 'result params))))))
   ((member method '("progress" "status"))
    (when (equal method "status")
      (pcase (alist-get 'stage params)
        ("embedding" (setq org-roam-retrieval--semantic-ready-p t))
        ((or "semantic-warming" "degraded")
         (setq org-roam-retrieval--semantic-ready-p nil))))
    (when-let* ((coverage (alist-get 'coverage params)))
      (let ((key (list (alist-get 'sources coverage) (alist-get 'chunks coverage)
                       (alist-get 'embedded_chunks coverage) (alist-get 'generation params)
                       org-roam-retrieval--semantic-ready-p)))
        (unless (equal key org-roam-retrieval--coverage-key)
          (setq org-roam-retrieval--coverage-key key
                org-roam-retrieval--refresh-lexical-p
                (and (equal method "progress")
                     (not (equal (alist-get 'state params) "complete"))
                     (not org-roam-retrieval--semantic-ready-p)))
          ;; A fixed timer coalesces bursts without waiting for the entire scan
          ;; or an idle editor.  Repeated OCR progress cannot postpone it.
          (unless (timerp org-roam-retrieval--refresh-timer)
            (setq org-roam-retrieval--refresh-timer
                  (run-at-time 0.5 nil #'org-roam-retrieval--refresh-visible)))))))))

(add-hook 'org-roam-retrieval-client-event-hook #'org-roam-retrieval--worker-event)

(defun org-roam-retrieval--source-kill ()
  "Cancel source-owned queries and release markers when its buffer closes."
  (dolist (session org-roam-retrieval--sessions)
    (when (and (eq (current-buffer) (org-roam-retrieval-session-source session))
               (org-roam-retrieval-ui--live-p session))
      (org-roam-retrieval-ui--cancel-session session)
      (set-marker (org-roam-retrieval-session-marker session) nil)
      (org-roam-retrieval-ui-state session "Source closed. Existing results remain available."))))

(defun org-roam-retrieval--search (text task scope label source root-ids append-p)
  "Search TEXT with TASK, SCOPE, LABEL, SOURCE, ROOT-IDS and APPEND-P."
  (when (string-empty-p (string-trim text)) (user-error "There is no text to search"))
  (let* ((buffer (current-buffer))
         (session (org-roam-retrieval-ui-create
                   buffer (copy-marker (point) t) text label
                   (if root-ids (string-join root-ids ", ") "all sources") append-p))
         (params `((text . ,text) (task . ,task) (scope . ,scope)
                   (source . ,source) (mode . ,org-roam-retrieval-search-mode)
                   (limit . ,org-roam-retrieval-result-limit)
                   ,@(when root-ids `((root_ids . ,(vconcat root-ids)))))))
    (add-hook 'kill-buffer-hook #'org-roam-retrieval--source-kill nil t)
    (setq org-roam-retrieval--sessions
          (cons session (cl-remove-if-not #'org-roam-retrieval-ui--live-p org-roam-retrieval--sessions)))
    (setf (org-roam-retrieval-session-refresh session)
          (lambda (&optional mode)
            (let ((request (copy-tree params)))
              (when mode (setf (alist-get 'mode request) mode))
              (org-roam-retrieval--submit session request))))
    (org-roam-retrieval--submit session params)
    session))

;;;###autoload
(defun org-roam-retrieval-search (query)
  "Search notes and configured document roots using plain-text QUERY."
  (interactive (list (read-string "Search Roam library: " (when (use-region-p)
                                                          (buffer-substring-no-properties
                                                           (region-beginning) (region-end))))))
  (org-roam-retrieval--search query "search" "query" "library query" nil nil nil))

;;;###autoload
(defun org-roam-retrieval-related (&optional all-roots)
  "Find notes related to the current node, selected region, or buffer.
With prefix ALL-ROOTS, include document roots as well as notes."
  (interactive "P")
  (let ((snapshot (org-roam-retrieval--snapshot)))
    (org-roam-retrieval--search
     (alist-get 'text snapshot) "related" (alist-get 'scope snapshot)
     (alist-get 'label snapshot) (alist-get 'source snapshot)
     (org-roam-retrieval--valid-related-roots all-roots) nil)))

;;;###autoload
(defun org-roam-retrieval-insert-list (&optional all-roots)
  "Append related-note links after retrieval, preserving concurrent edits.
With prefix ALL-ROOTS, include document sources.  Search runs without opening
results.  Changed sources are left untouched and reported in the echo area.
No source buffer is saved."
  (interactive "P" org-mode)
  (unless (derived-mode-p 'org-mode) (user-error "List insertion requires an Org buffer"))
  (barf-if-buffer-read-only)
  (let ((snapshot (org-roam-retrieval--snapshot)))
    (org-roam-retrieval--search
     (alist-get 'text snapshot) "related" (alist-get 'scope snapshot)
     (alist-get 'label snapshot) (alist-get 'source snapshot)
     (org-roam-retrieval--valid-related-roots all-roots) t)))

;;;###autoload
(defun org-roam-retrieval-cancel ()
  "Cancel retrieval in this result pane or searches from the current source."
  (interactive)
  (if (derived-mode-p 'org-roam-retrieval-results-mode)
      (org-roam-retrieval-ui-cancel)
    (dolist (session org-roam-retrieval--sessions)
      (when (and (eq (current-buffer) (org-roam-retrieval-session-source session))
                 (org-roam-retrieval-ui--live-p session))
        (with-current-buffer (org-roam-retrieval-session-buffer session)
          (org-roam-retrieval-ui-cancel))))))

(defvar org-roam-retrieval-status-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'org-roam-retrieval-status)
    (define-key map (kbd "s") #'org-roam-retrieval-setup)
    (define-key map (kbd "r") #'org-roam-retrieval-reconcile)
    (define-key map (kbd "R") #'org-roam-retrieval-repair)
    map))

(define-derived-mode org-roam-retrieval-status-mode special-mode "Roam Retrieval Status"
  "Worker readiness, index coverage and explicit recovery actions.")

(defun org-roam-retrieval--status-display (text &optional show)
  "Render TEXT in the status screen.  SHOW explicitly opens it."
  (when-let* ((buffer (if show (get-buffer-create "*Org-roam retrieval status*")
                       (get-buffer "*Org-roam retrieval status*"))))
    (with-current-buffer buffer
      (org-roam-retrieval-status-mode)
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Org-roam retrieval status\n\ng refresh · s setup · r reconcile sources · R rebuild derived index · q close\n\n" text)
        (goto-char (point-min))))
    (when show (display-buffer buffer))))

(defun org-roam-retrieval--status-if-visible ()
  "Refresh an existing visible status pane without reopening one."
  (when-let* ((buffer (get-buffer "*Org-roam retrieval status*"))
              (window (get-buffer-window buffer t)))
    (org-roam-retrieval-status)))

(defun org-roam-retrieval--status-text (status)
  "Render worker STATUS without exposing source contents."
  (let* ((readiness (alist-get 'readiness status))
         (coverage (alist-get 'coverage status))
         (errors (append (alist-get 'errors status) nil)))
    (concat
     (format "Lexical: %s\nSemantic: %s\nModel loaded: %s\n\n"
             (if (eq t (alist-get 'lexical readiness)) "ready" "not ready")
             (if (eq t (alist-get 'semantic readiness)) "ready" "unavailable / warming")
             (if (eq t (alist-get 'model_loaded readiness)) "yes" "no"))
     (format "Sources: %s\nEligible passages: %s\nEmbedded passages: %s\nQueued jobs: %s\n"
             (or (alist-get 'sources coverage) 0) (or (alist-get 'eligible_chunks coverage) (alist-get 'chunks coverage) 0)
             (or (alist-get 'embedded_chunks coverage) 0) (or (alist-get 'backlog status) 0))
     (format "Sources awaiting extraction: %s\nPassages awaiting embeddings: %s\nFilename-only sources: %s\nExcluded generated/build sources: %s\n"
             (or (alist-get 'pending_extraction_sources coverage) 0)
             (or (alist-get 'pending_embedding_chunks coverage) 0)
             (or (alist-get 'filename_only_sources coverage) 0)
             (or (alist-get 'excluded_sources coverage) 0))
     (when-let* ((degraded (alist-get 'degraded status))) (format "\n%s\n" degraded))
     (when errors
       (concat "\nSources needing attention:\n"
               (mapconcat (lambda (entry) (format "• %s: %s" (alist-get 'path entry) (alist-get 'error entry)))
                          errors "\n"))))))

;;;###autoload
(defun org-roam-retrieval-status ()
  "Show worker readiness, index coverage and repair actions."
  (interactive)
  (org-roam-retrieval--status-display "Checking worker…" t)
  (org-roam-retrieval-client-request
   "status" nil
   (lambda (status) (org-roam-retrieval--status-display (org-roam-retrieval--status-text status)))
   (lambda (failure)
     (org-roam-retrieval--status-display
      (format "Worker unavailable: %s\n\nPress s to provision/check its environment.\n"
              (alist-get 'message failure))))))

;;;###autoload
(defun org-roam-retrieval-setup ()
  "Provision the managed worker environment, then show its status."
  (interactive)
  (org-roam-retrieval--status-display "Preparing the managed worker environment…" t)
  (org-roam-retrieval-client-setup
   (lambda (&rest _) (org-roam-retrieval--status-if-visible))
   (lambda (failure)
     (org-roam-retrieval--status-display (format "Setup failed: %s\n" (alist-get 'message failure))))))

;;;###autoload
(defun org-roam-retrieval-reconcile ()
  "Schedule reconciliation of configured sources without blocking Emacs."
  (interactive)
  (org-roam-retrieval-client-request
   "reconcile" nil
   (lambda (_job) (org-roam-retrieval--status-if-visible))
   (lambda (failure) (message "Reconciliation failed: %s" (alist-get 'message failure)))))

;;;###autoload
(defun org-roam-retrieval-repair ()
  "Preview and explicitly rebuild only the derived retrieval index."
  (interactive)
  (org-roam-retrieval--status-display "Checking the derived-index repair plan…" t)
  (org-roam-retrieval-client-request
   "repair" '((action . "rebuild") (dry_run . t))
   (lambda (response)
     (let* ((plan (alist-get 'plan response))
            (preserves (append (alist-get 'preserves plan) nil)))
       (when (get-buffer-window "*Org-roam retrieval status*" t)
         (org-roam-retrieval--status-display
          (format "Repair plan\n\nPreserves: %s\nRebuilds the derived source/chunk index.\n"
                  (string-join preserves ", ")))
         (when (yes-or-no-p "Rebuild the derived retrieval index while preserving sources and graph? ")
           (org-roam-retrieval-client-request
            "repair" '((action . "rebuild") (dry_run . :false))
            (lambda (_job) (org-roam-retrieval--status-if-visible))
            (lambda (failure) (message "Repair failed: %s" (alist-get 'message failure))))))))
   (lambda (failure)
     (org-roam-retrieval--status-display (format "Repair planning failed: %s" (alist-get 'message failure))))))

;; Command continuity, without loading either replaced package.
(defalias 'org-similarity-sidebuffer #'org-roam-retrieval-related)
(defalias 'org-similarity-query #'org-roam-retrieval-search)
(defalias 'org-similarity-insert-list #'org-roam-retrieval-insert-list)
(defalias 'org-similarity-cancel #'org-roam-retrieval-cancel)
(defalias 'vdeft #'org-roam-retrieval-search)
(defalias 'vdeft-similar-sidebuffer #'org-roam-retrieval-related)
(defalias 'vdeft-insert-similar-list #'org-roam-retrieval-insert-list)

(defun org-roam-retrieval--pending-p (session)
  "Whether SESSION has active query or progressive-index work to cancel."
  (and (org-roam-retrieval-ui--live-p session)
       (or (org-roam-retrieval-session-request-id session)
           (eq t (alist-get 'semantic_pending (org-roam-retrieval-session-response session)))
           (eq t (alist-get 'indexing (org-roam-retrieval-session-response session))))))

(defun org-roam-retrieval-command-available-p (command buffer)
  "Whether COMMAND is useful in BUFFER for contextual M-x completion."
  (with-current-buffer buffer
    (let ((session (and (derived-mode-p 'org-roam-retrieval-results-mode)
                        org-roam-retrieval-ui--session)))
      (pcase command
        ((or 'org-roam-retrieval-insert-list 'org-similarity-insert-list
             'vdeft-insert-similar-list)
         (and (derived-mode-p 'org-mode) (not buffer-read-only) (> (buffer-size) 0)))
        ((or 'org-roam-retrieval-related 'org-similarity-sidebuffer 'vdeft-similar-sidebuffer)
         (and (derived-mode-p 'text-mode 'prog-mode 'fundamental-mode)
              (> (buffer-size) 0)))
        ('org-roam-retrieval-ui-open
         (and session (org-roam-retrieval-ui--current-result)))
        ('org-roam-retrieval-ui-insert
         (let ((marker (and session (org-roam-retrieval-session-marker session))))
           (and marker (marker-buffer marker) (org-roam-retrieval-ui--current-result)
                (not (buffer-local-value 'buffer-read-only (marker-buffer marker))))))
        ((or 'org-roam-retrieval-ui-next 'org-roam-retrieval-ui-previous)
         (and session (org-roam-retrieval-session-results session)))
        ('org-roam-retrieval-ui-refresh
         (and session (buffer-live-p (org-roam-retrieval-session-source session))))
        ('org-roam-retrieval-ui-quit session)
        ((or 'org-roam-retrieval-ui-cancel 'org-roam-retrieval-cancel 'org-similarity-cancel)
         (if session (org-roam-retrieval--pending-p session)
           (cl-some (lambda (query)
                      (and (eq buffer (org-roam-retrieval-session-source query))
                           (org-roam-retrieval--pending-p query)))
                    org-roam-retrieval--sessions)))
        ('org-roam-retrieval-client-stop
         (process-live-p org-roam-retrieval-client--process))
        ('org-roam-retrieval-client-disable org-roam-retrieval-client--enabled)
        (_ t)))))

(dolist (command '(org-roam-retrieval-insert-list org-similarity-insert-list
                   vdeft-insert-similar-list org-roam-retrieval-related
                   org-similarity-sidebuffer vdeft-similar-sidebuffer
                   org-roam-retrieval-ui-open org-roam-retrieval-ui-insert
                   org-roam-retrieval-ui-next org-roam-retrieval-ui-previous
                   org-roam-retrieval-ui-refresh org-roam-retrieval-ui-quit
                   org-roam-retrieval-ui-cancel org-roam-retrieval-cancel
                   org-similarity-cancel org-roam-retrieval-client-stop
                   org-roam-retrieval-client-disable))
  (put command 'completion-predicate #'org-roam-retrieval-command-available-p))

(provide 'org-roam-retrieval)
;;; org-roam-retrieval.el ends here
