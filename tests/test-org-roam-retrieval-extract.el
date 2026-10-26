;;; test-org-roam-retrieval-extract.el --- Saved Org extraction checks -*- lexical-binding: t; -*-
(require 'ert)
(require 'org-roam-retrieval-extract)

(defmacro org-roam-retrieval-test-org (text &rest body)
  "Run BODY with a temporary saved Org file containing TEXT."
  (declare (indent 1))
  `(let ((file (make-temp-file "roam extract ' " nil ".org")))
     (unwind-protect
         (progn (with-temp-file file (insert ,text)) ,@body)
       (delete-file file))))

(ert-deftest org-roam-retrieval-extract-node-identity-and-nonoverlap ()
  (org-roam-retrieval-test-org
      ":PROPERTIES:\n:ID: file-node\n:END:\n#+title: Astronomy\nFile prose.\n* Parent\n:PROPERTIES:\n:ID: parent-node\n:END:\nParent prose.\n** Child without ID\nChild prose.\n** Child node\n:PROPERTIES:\n:ID: child-node\n:END:\nIndependent child prose.\n"
    (let* ((result (org-roam-retrieval-extract-file file))
           (segments (append (alist-get 'segments result) nil)))
      (should (equal "Astronomy" (alist-get 'title result)))
      (should (= 4 (length segments)))
      (should (equal '("file-node" "parent-node" "parent-node" "child-node")
                     (mapcar (lambda (s) (alist-get 'id (alist-get 'locator s))) segments)))
      (should (equal "Parent prose." (alist-get 'text (nth 1 segments))))
      (should (equal "Child prose." (alist-get 'text (nth 2 segments))))
      (should (equal "org-section" (alist-get 'kind (alist-get 'locator (nth 2 segments)))))
      (should (equal ["Parent" "Child without ID"]
                     (alist-get 'outline (alist-get 'locator (nth 2 segments)))))
      (should (> (alist-get 'line (alist-get 'locator (nth 3 segments))) 1)))))

(ert-deftest org-roam-retrieval-extract-filters-metadata-and-babel ()
  (org-roam-retrieval-test-org
      "#+title: Test\n#+filetags: secretmetadata\n:PROPERTIES:\n:ID: ignoremisplaceddrawer\n:END:\nText [[id:secret-target][visible description]] and *bold*.\n#+begin_src emacs-lisp\n(error \"must never run\")\n#+end_src\n#+begin_example\nsecret-example\n#+end_example\n# secret-comment\n"
    (let ((text (alist-get 'text (aref (alist-get 'segments (org-roam-retrieval-extract-file file)) 0))))
      (should (string-match-p "visible description" text))
      (should (string-match-p "bold" text))
      (dolist (secret '("secretmetadata" "secret-target" "must never run" "secret-example" "secret-comment"))
        (should-not (string-match-p secret text))))))

(ert-deftest org-roam-retrieval-extract-never-reads-unsaved-buffer ()
  (org-roam-retrieval-test-org "#+title: Saved\nSaved prose.\n"
    (let ((buffer (find-file-noselect file)))
      (unwind-protect
          (with-current-buffer buffer
            (erase-buffer) (insert "Unsaved replacement")
            (let ((result (org-roam-retrieval-extract-file file)))
              (should (equal "Saved" (alist-get 'title result)))
              (should (equal "Saved prose." (alist-get 'text (aref (alist-get 'segments result) 0)))))
            (should (buffer-modified-p)))
        (with-current-buffer buffer (set-buffer-modified-p nil))
        (kill-buffer buffer)))))

(ert-deftest org-roam-retrieval-extract-batch-unicode-and-errors ()
  (org-roam-retrieval-test-org "#+title: שלום Café\nκόσμος שלום café\n"
    (let ((request (make-temp-file "roam-request" nil ".json"))
          (output (generate-new-buffer " *roam-extract-output*")))
      (unwind-protect
          (progn
            (with-temp-file request
              (set-buffer-file-coding-system 'utf-8-unix)
              (insert (decode-coding-string
                       (json-serialize `((files . [,file "/does/not/exist.org"]))) 'utf-8)))
            (let ((standard-output output)) (org-roam-retrieval-extract-batch request))
            (with-current-buffer output
              (goto-char (point-min))
              (let ((response (json-parse-buffer :object-type 'alist :array-type 'list)))
                (should (= 1 (length (alist-get 'errors response))))
                (should (= 1 (length (alist-get 'sources response))))
                (should (equal "שלום Café" (alist-get 'title (car (alist-get 'sources response))))))))
        (delete-file request) (kill-buffer output)))))

(ert-deftest org-roam-retrieval-extract-title-only-nodes ()
  (org-roam-retrieval-test-org
      ":PROPERTIES:\n:ID: file-id\n:END:\n#+title: Title only\n* Heading only\n** Nested heading\n:PROPERTIES:\n:ID: nested-id\n:END:\n"
    (let* ((source (org-roam-retrieval-extract-file file))
           (segments (append (alist-get 'segments source) nil)))
      (should (= 3 (length segments)))
      (should (equal '("Title only" "Heading only" "Nested heading")
                     (mapcar (lambda (segment) (alist-get 'text segment)) segments)))
      (should (equal '("file-id" "file-id" "nested-id")
                     (mapcar (lambda (segment) (alist-get 'id (alist-get 'locator segment))) segments))))))

(ert-deftest org-roam-retrieval-extract-heading-id-is-not-a-file-id ()
  (org-roam-retrieval-test-org "* First node\n:PROPERTIES:\n:ID: heading-id\n:END:\nText.\n"
    (let ((source (org-roam-retrieval-extract-file file)))
      (should-not (alist-get 'id source))
      (should (equal "heading-id" (alist-get 'id (alist-get 'locator (aref (alist-get 'segments source) 0))))))))

(ert-deftest org-roam-retrieval-extract-text-cleans-unsaved-snapshot ()
  (let ((text (org-roam-retrieval-extract-text
               "#+title: Query Title\n* Heading name\n:PROPERTIES:\n:ID: hidden-id\n:END:\nשלום [[id:hidden-target][visible name]]\n#+begin_src text\nsecret code\n#+end_src\n")))
    (should (string-match-p "Query Title" text))
    (should (string-match-p "Heading name" text))
    (should (string-match-p "שלום visible name" text))
    (should-not (string-match-p "hidden-id\\|hidden-target\\|secret code" text))))

(ert-deftest org-roam-retrieval-extract-mixed-encoding-produces-valid-json ()
  (let ((file (make-temp-file "roam-mixed-encoding" nil ".org")))
    (unwind-protect
        (progn
          (with-temp-file file
            (set-buffer-multibyte nil)
            (insert (encode-coding-string "#+title: Mixed שלום\nUnicode “quoted” prose " 'utf-8-unix))
            (insert (unibyte-string 255)))
          (let* ((source (org-roam-retrieval-extract-file file))
                 (serialized (json-serialize source :null-object nil))
                 (roundtrip (json-parse-string serialized :object-type 'alist)))
            (should (equal "Mixed שלום" (alist-get 'title roundtrip)))
            (should (string-match-p "“quoted”" (alist-get 'text (aref (alist-get 'segments source) 0))))
            (should (string-match-p "�" (alist-get 'text (aref (alist-get 'segments source) 0))))))
      (delete-file file))))

(ert-deftest org-roam-retrieval-extract-keeps-original-inline-and-environment-math ()
  (let* ((math "Inline $x^2+y^2$ and equation:\n\\begin{equation}\na^2+b^2=c^2\n\\end{equation}\n")
         (text (org-roam-retrieval-extract-text math)))
    (should (string-match-p (regexp-quote "$x^2+y^2$") text))
    (should (string-match-p (regexp-quote "a^2+b^2=c^2") text))))
