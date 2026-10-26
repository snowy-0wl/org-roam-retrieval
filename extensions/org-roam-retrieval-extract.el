;;; org-roam-retrieval-extract.el --- Saved Org prose extraction -*- lexical-binding: t; -*-

;; Copyright (C) 2026
;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Worker-facing, saved-file extraction using Org's parser.  This helper never
;; loads user init, evaluates Babel, changes authored IDs, or visits live buffers.

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'org)
(require 'org-element)
(require 'subr-x)

(defun org-roam-retrieval-extract--unicode (text)
  "Return Unicode TEXT, decoding UTF-8 bytes and replacing malformed bytes.
Org snapshots can contain raw eight-bit characters after a mixed-encoding
paste.  Such characters are not valid JSON string values."
  (replace-regexp-in-string
   (concat "[" (string #x3fff80) "-" (string #x3fffff) "]") "�"
   (decode-coding-string (encode-coding-string text 'utf-8-unix) 'utf-8-unix)
   t t))

(defun org-roam-retrieval-extract--prose (element)
  "Render searchable prose from Org ELEMENT, without executable metadata."
  (cond
   ((stringp element) (substring-no-properties element))
   ((memq (org-element-type element)
          '(property-drawer drawer src-block inline-src-block example-block
            export-block keyword comment comment-block planning clock
            timestamp babel-call inline-babel-call footnote-reference
            citation citation-reference statistics-cookie)) "")
   ((eq (org-element-type element) 'link)
    (mapconcat #'org-roam-retrieval-extract--prose (org-element-contents element) ""))
   ((eq (org-element-type element) 'entity)
    (or (org-element-property :utf-8 element) ""))
   ((memq (org-element-type element) '(latex-fragment latex-environment))
    (or (org-element-property :value element) ""))
   ((eq (org-element-type element) 'code)
    (or (org-element-property :value element) ""))
   ((eq (org-element-type element) 'verbatim)
    (or (org-element-property :value element) ""))
   (t
    (mapconcat #'org-roam-retrieval-extract--prose
               (org-element-contents element)
               (if (memq (org-element-type element)
                         '(section paragraph plain-list item table table-row table-cell
                           quote-block verse-block center-block special-block))
                   " " "")))))

(defun org-roam-retrieval-extract-text (text)
  "Return searchable prose from unsaved Org TEXT without file side effects.
Titles and heading names remain query signals; properties and code do not."
  (with-temp-buffer
    (insert (org-roam-retrieval-extract--unicode text))
    (setq-local change-major-mode-hook nil)
    (let ((org-inhibit-startup t))
      (delay-mode-hooks (org-mode)))
    (let* ((tree (org-element-parse-buffer))
           (title (cadar (org-collect-keywords '("TITLE"))))
           (headings (org-element-map tree 'headline
                       (lambda (headline) (org-element-property :raw-value headline)))))
      (string-trim
       (replace-regexp-in-string
        "[ \t\r\n]+" " "
        (string-join (append (when title (list title)) headings
                             (list (org-roam-retrieval-extract--prose tree))) " "))))))

(defun org-roam-retrieval-extract--outline (element)
  "Return heading ancestry for ELEMENT, ordered from outer to inner."
  (let ((parent (org-element-property :parent element)) outline)
    (while parent
      (when (eq (org-element-type parent) 'headline)
        (push (org-element-property :raw-value parent) outline))
      (setq parent (org-element-property :parent parent)))
    outline))

(defun org-roam-retrieval-extract--node (section file-id)
  "Return (ID . POSITION) for SECTION's nearest authored node or FILE-ID."
  (let ((parent (org-element-property :parent section)) found)
    (while (and parent (not found))
      (when (eq (org-element-type parent) 'headline)
        (let ((position (org-element-property :begin parent)))
          (when-let* ((id (org-entry-get position "ID")))
            (setq found (cons id position)))))
      (setq parent (org-element-property :parent parent)))
    (or found (and file-id (cons file-id 1)))))

(defun org-roam-retrieval-extract--segment (text title file-id heading position)
  "Build a prose segment from TEXT, TITLE, FILE-ID, HEADING and POSITION."
  (let* ((outline (when heading
                    (append (org-roam-retrieval-extract--outline heading)
                            (list (org-element-property :raw-value heading)))))
         (own-id (when heading (org-entry-get (org-element-property :begin heading) "ID")))
         (node (or (and own-id (cons own-id (org-element-property :begin heading)))
                   (when heading (org-roam-retrieval-extract--node heading file-id))
                   (and file-id (cons file-id 1)))))
    `((text . ,text)
      (title . ,(if outline (string-join (cons title outline) " / ") title))
      (locator . ((kind . ,(if (or own-id (and (null heading) file-id)) "org-node" "org-section"))
                  (id . ,(car node)) (outline . ,(vconcat outline))
                  (position . ,position) (line . ,(line-number-at-pos position)))))))

(defun org-roam-retrieval-extract-file (file)
  "Read saved FILE and return its title and non-overlapping prose segments.
Each locator uses one-based Emacs character positions and line numbers.
IDs are existing file/heading IDs only.  No IDs are created by indexing."
  (unless (and (stringp file) (not (file-remote-p file)))
    (error "Org extraction requires a local file"))
  (with-temp-buffer
    (let ((coding-system-for-read 'utf-8-unix))
      (insert-file-contents file))
    (let ((clean (org-roam-retrieval-extract--unicode (buffer-string))))
      (erase-buffer) (insert clean))
    (let ((org-inhibit-startup t)
          (org-mode-hook nil)
          (org-confirm-babel-evaluate t)
          (enable-local-variables nil)
          (enable-local-eval nil))
      (org-mode))
    (let* ((title (or (cadar (org-collect-keywords '("TITLE"))) (file-name-base file)))
           (file-id (save-excursion
                      (goto-char (point-min))
                      (unless (org-at-heading-p) (org-entry-get (point-min) "ID"))))
           (tree (org-element-parse-buffer))
           (seen (make-hash-table :test #'eql)) segments)
      (org-element-map tree 'section
        (lambda (section)
          (let* ((text (string-trim
                        (replace-regexp-in-string
                         "[ \t\r\n]+" " " (org-roam-retrieval-extract--prose section))))
                 (parent (org-element-property :parent section))
                 (heading (when (eq (org-element-type parent) 'headline) parent))
                 (position (org-element-property :begin section)))
            (when heading (puthash (org-element-property :begin heading) t seen))
            (when (string-empty-p text)
              (setq text (if heading (org-element-property :raw-value heading) title)))
            (push (org-roam-retrieval-extract--segment text title file-id heading position) segments))))
      ;; Headings without an immediate section still represent searchable nodes.
      (org-element-map tree 'headline
        (lambda (heading)
          (let ((position (org-element-property :begin heading)))
            (unless (gethash position seen)
              (push (org-roam-retrieval-extract--segment
                     (org-element-property :raw-value heading) title file-id heading position) segments)))))
      (unless segments
        (push (org-roam-retrieval-extract--segment title title file-id nil 1) segments))
      (setq segments (sort segments
                           (lambda (a b)
                             (< (alist-get 'position (alist-get 'locator a))
                                (alist-get 'position (alist-get 'locator b))))))
      `((path . ,(file-truename file)) (title . ,title) (id . ,file-id)
        (segments . ,(vconcat segments))))))

(defun org-roam-retrieval-extract-batch (request-file)
  "Read JSON REQUEST-FILE containing files and write a UTF-8 JSON response.
Errors are isolated per source rather than discarding successful extractions."
  (set-terminal-coding-system 'utf-8-unix)
  (let* ((request (with-temp-buffer
                    (insert-file-contents request-file)
                    (json-parse-buffer :object-type 'alist :array-type 'list
                                       :null-object nil :false-object nil)))
         sources errors)
    (dolist (file (alist-get 'files request))
      (condition-case err
          (let ((source (org-roam-retrieval-extract-file file)))
            ;; Validate each source before assembling the response, isolating bad data.
            (unless (> (string-bytes (json-serialize source :null-object nil)) 0)
              (error "Empty extraction response"))
            (push source sources))
        (error (push `((path . ,file)
                       (message . ,(format "Org extraction failed (%s)" (car err)))) errors))))
    (princ (decode-coding-string
            (json-serialize `((sources . ,(vconcat (nreverse sources)))
                              (errors . ,(vconcat (nreverse errors))))
                            :null-object nil)
            'utf-8))))

(provide 'org-roam-retrieval-extract)
;;; org-roam-retrieval-extract.el ends here
