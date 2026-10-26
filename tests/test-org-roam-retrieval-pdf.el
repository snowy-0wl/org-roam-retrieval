;;; test-org-roam-retrieval-pdf.el --- Isolated graphical PDF integration -*- lexical-binding: t; -*-

;; Run only in an isolated graphical Emacs started with -Q and the package
;; test bootstrap.  Set ORG_ROAM_RETRIEVAL_TEST_PDF to a PDF with >= 3 pages.
;; No user init is needed, and the source PDF is never saved or annotated.

(require 'ert)
(require 'org-roam-retrieval-ui)
(declare-function pdf-view-current-page "pdf-view" (&optional window))

(ert-deftest org-roam-retrieval-pdf-real-page-three-and-readable-link ()
  (let ((file (getenv "ORG_ROAM_RETRIEVAL_TEST_PDF")))
    (skip-unless (and file (file-readable-p file) (display-graphic-p)))
    (require 'pdf-tools)
    (let ((before (with-temp-buffer
                    (insert-file-contents-literally file)
                    (secure-hash 'sha256 (current-buffer))))
          (existing (find-buffer-visiting file))
          (auto-mode-alist (cons '("\\.pdf\\'" . pdf-view-mode) auto-mode-alist)))
      (unwind-protect
          (progn
            (org-roam-retrieval-ui-open-location file '((kind . "pdf-page") (page . 3)))
            (should (derived-mode-p 'pdf-view-mode))
            (should (= 3 (pdf-view-current-page)))
            (org-roam-retrieval-ui-follow-pdf (concat (org-link-escape file) "::3") nil)
            (should (= 3 (pdf-view-current-page)))
            (should (equal before (with-temp-buffer
                                    (insert-file-contents-literally file)
                                    (secure-hash 'sha256 (current-buffer))))))
        (unless existing
          (when-let* ((buffer (find-buffer-visiting file)))
            (with-current-buffer buffer (set-buffer-modified-p nil))
            (kill-buffer buffer)))))))
