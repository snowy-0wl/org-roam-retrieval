;;; test-org-roam-personal-extraction.el --- Preserve personal subtree fix -*- lexical-binding: t; -*-
(require 'ert)
(require 'cl-lib)
(require 'org-roam)

(ert-deftest org-roam-personal-promote-preserves-planning-and-id ()
  (with-temp-buffer
    (org-mode)
    (insert "* TODO Example :tag:\nSCHEDULED: <2026-10-25 Sun> DEADLINE: <2026-10-26 Mon> CLOSED: [2026-10-24 Sat]\n:PROPERTIES:\n:ID: stable-id\n:END:\n\nBody text.\n** Child\nChild body.\n")
    (goto-char (point-min))
    (org-roam--promote-entire-buffer-internal)
    (goto-char (point-min))
    (should (equal "stable-id" (org-entry-get (point) "ID")))
    (dolist (line '(":SCHEDULED: <2026-10-25 Sun>"
                    ":DEADLINE: <2026-10-26 Mon>"
                    ":CLOSED: [2026-10-24 Sat]"))
      (should (string-match-p (regexp-quote line) (buffer-string))))
    (should (string-match-p (regexp-quote "#+title: Example\n\n") (buffer-string)))
    (should (string-match-p "^\\* Child" (buffer-string)))))

(ert-deftest org-roam-personal-extract-refreshes-source-after-removal ()
  (let* ((root (make-temp-file "roam-extraction-" t))
         (org-roam-directory root)
         (source (expand-file-name "source.org" root))
         (destination (expand-file-name "extracted.org" root))
         (updates nil))
    (unwind-protect
        (progn
          (with-temp-file source
            (insert ":PROPERTIES:\n:ID: source-id\n:END:\n#+title: Source\n\n* Child\n:PROPERTIES:\n:ID: child-id\n:END:\nBody.\n"))
          (with-current-buffer (find-file-noselect source)
            (goto-char (point-min)) (search-forward "* Child") (beginning-of-line)
            (cl-letf (((symbol-function 'read-file-name) (lambda (&rest _) destination))
                      ((symbol-function 'org-roam-node-at-point)
                       (lambda (&rest _) (org-roam-node-create :id "child-id" :title "Child")))
                      ((symbol-function 'org-roam-db-update-file)
                       (lambda (&rest _)
                         (when (equal buffer-file-name source)
                           (push (buffer-string) updates)))))
              (org-roam-extract-subtree)))
          (should (= 2 (length updates)))
          (should-not (string-match-p "child-id" (car updates)))
          (should (string-match-p "child-id" (cadr updates)))
          (should (file-exists-p destination)))
      (dolist (file (list source destination))
        (when-let* ((buffer (find-buffer-visiting file))) (kill-buffer buffer)))
      (delete-directory root t))))
