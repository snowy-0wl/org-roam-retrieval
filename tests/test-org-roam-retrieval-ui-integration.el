;;; test-org-roam-retrieval-ui-integration.el --- Real worker/UI checks -*- lexical-binding: t; -*-
(require 'ert)
(require 'org-roam-retrieval)

(defun org-roam-retrieval-test-await (predicate &optional seconds)
  "Wait for PREDICATE for SECONDS in test code only."
  (let ((deadline (+ (float-time) (or seconds 20))))
    (while (and (not (funcall predicate)) (< (float-time) deadline))
      (accept-process-output nil 0.05))
    (should (funcall predicate))))

(ert-deftest org-roam-retrieval-ui-real-worker-search-open-and-insert ()
  (let ((python (getenv "ORG_ROAM_RETRIEVAL_TEST_PYTHON")))
    (skip-unless (and python (file-executable-p python)))
    (let* ((temporary (make-temp-file "roam retrieval integration ' " t))
           (notes (expand-file-name "notes" temporary))
           (file (expand-file-name "שלום.org" notes))
           (source (generate-new-buffer " *retrieval-integration-source*"))
           (org-roam-retrieval-python-program python)
           (org-roam-retrieval-cache-directory (expand-file-name "cache" temporary))
           (org-roam-retrieval-database-file (expand-file-name "index.sqlite" temporary))
           (org-roam-retrieval-model "google/embeddinggemma-2")
           (org-roam-retrieval-model-revision "0000000000000000000000000000000000000000")
           (org-roam-retrieval-search-mode "lexical")
           (org-roam-retrieval-roots `(((id . "org-roam") (path . ,notes))))
           (org-roam-retrieval--sessions nil)
           (org-id-locations nil) (org-id-files nil)
           session)
      (unwind-protect
          (progn
            (make-directory notes)
            (with-temp-file file
              (insert "#+title: Café שלום\n* Exact node\n:PROPERTIES:\n:ID: exact-node\n:END:\nκόσμος astronomy galaxy\n"))
            (with-temp-file (expand-file-name "unrelated.org" notes)
              (insert "#+title: Bread\nSourdough flour baking\n"))
            (org-roam-retrieval-client-start)
            (org-roam-retrieval-test-await
             (lambda () (eq org-roam-retrieval-client-state 'ready)))
            ;; Wait for actual saved corpus indexing, not merely worker startup.
            (let (indexed)
              (org-roam-retrieval-test-await
               (lambda ()
                 (unless indexed
                   (org-roam-retrieval-client-request
                    "status" nil
                    (lambda (status)
                      (setq indexed (>= (or (alist-get 'sources (alist-get 'coverage status)) 0) 2)))
                    #'ignore))
                 indexed)))
            (with-current-buffer source
              (org-mode) (insert "Draft ")
              (setq session (org-roam-retrieval-search "κόσμος")))
            (org-roam-retrieval-test-await
             (lambda () (org-roam-retrieval-session-results session)))
            (let* ((result (car (org-roam-retrieval-session-results session)))
                   (locator (alist-get 'locator result)))
              (should (equal (file-truename file) (alist-get 'path result)))
              (should (equal "exact-node" (alist-get 'id locator)))
              (should (equal "org-node" (alist-get 'kind locator))))
            (with-current-buffer (org-roam-retrieval-session-buffer session)
              (should (string-match-p "Café שלום" (buffer-string)))
              (org-roam-retrieval-ui-insert)
              (org-roam-retrieval-ui-open))
            (with-current-buffer source
              (should (string-match-p "\\[\\[id:exact-node\\]" (buffer-string))))
            (with-current-buffer (find-buffer-visiting file)
              (should (equal (org-entry-get nil "ID") "exact-node"))))
        (org-roam-retrieval-client-stop)
        (when (timerp org-roam-retrieval--refresh-timer)
          (cancel-timer org-roam-retrieval--refresh-timer)
          (setq org-roam-retrieval--refresh-timer nil))
        (when session
          (when (buffer-live-p (org-roam-retrieval-session-buffer session))
            (kill-buffer (org-roam-retrieval-session-buffer session))))
        (when (buffer-live-p source) (kill-buffer source))
        (when-let* ((buffer (find-buffer-visiting file))) (kill-buffer buffer))
        (delete-directory temporary t)))))
