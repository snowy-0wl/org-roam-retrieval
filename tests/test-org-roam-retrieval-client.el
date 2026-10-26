;;; test-org-roam-retrieval-client.el --- Worker client checks -*- lexical-binding: t; -*-
(require 'ert)
(require 'cl-lib)
(require 'org-roam-retrieval-client)

(defmacro org-roam-retrieval-test-client (&rest body)
  "Run BODY against an isolated real JSON-RPC subprocess."
  (declare (indent 0))
  `(let* ((directory (make-temp-file "roam client ' " t))
          (org-roam-retrieval-client--package-directory directory)
          (org-roam-retrieval-python-program (executable-find "python3"))
          (org-roam-retrieval-cache-directory (expand-file-name "cache" directory))
          (org-roam-retrieval-client--pending (make-hash-table :test #'eql))
          (org-roam-retrieval-client--queue nil)
          (org-roam-retrieval-client--process nil)
          (org-roam-retrieval-client--sequence 0)
          (org-roam-retrieval-client-state 'stopped)
          (org-roam-retrieval-client-last-error nil)
          (org-roam-retrieval-client--dirty (make-hash-table :test #'equal))
          (org-roam-retrieval-client--dirty-timer nil)
          (org-roam-retrieval-client--enabled nil)
          (org-roam-retrieval-client-event-hook nil)
          (org-roam-retrieval-request-timeout 2))
     (unwind-protect
         (progn
           (make-directory (expand-file-name "python" directory))
           (with-temp-file (expand-file-name "python/org_roam_retrieval.py" directory)
             (insert "import sys,json,time,os\nlast_cancel=None\n"
                     "for line in sys.stdin:\n"
                     " r=json.loads(line); m=r['method']; i=r.get('id'); p=r.get('params',{})\n"
                     " if m=='cancel': last_cancel=p['request_id']; continue\n"
                     " if m=='crash': os._exit(3)\n"
                     " if m=='sleep': time.sleep(20)\n"
                     " if m=='notify': print(json.dumps({'jsonrpc':'2.0','method':'progress','params':{'state':'ready'}}),flush=True)\n"
                     " out={'jsonrpc':'2.0','id':i}\n"
                     " if m=='fail': out['error']={'code':42,'message':'Expected failure'}\n"
                     " else: out['result']={'method':m,'text':p.get('text',''),'ready':True,'cancelled':last_cancel}\n"
                     " data=json.dumps(out,ensure_ascii=False)+'\\n'\n"
                     " sys.stdout.write(data[:4]); sys.stdout.flush(); sys.stdout.write(data[4:]); sys.stdout.flush()\n"))
           ,@body)
       (org-roam-retrieval-client-disable)
       (delete-directory directory t))))

(defun org-roam-retrieval-test-wait (predicate)
  "Wait briefly for PREDICATE to become true."
  (let ((deadline (+ (float-time) 5)))
    (while (and (not (funcall predicate)) (< (float-time) deadline))
      (accept-process-output nil 0.01))
    (should (funcall predicate))))

(ert-deftest org-roam-retrieval-client-queues-initialization-and-unicode ()
  (org-roam-retrieval-test-client
    (let (responses errors)
      (org-roam-retrieval-client-request
       "search" '((text . "שלום café κόσμος"))
       (lambda (value) (push value responses)) (lambda (value) (push value errors)))
      (org-roam-retrieval-client-request
       "search" '((text . "Second"))
       (lambda (value) (push value responses)) (lambda (value) (push value errors)))
      (org-roam-retrieval-test-wait (lambda () (= (length responses) 2)))
      (should-not errors)
      (should (eq org-roam-retrieval-client-state 'ready))
      (should (equal (alist-get 'text (car responses)) "Second"))
      (should (equal (alist-get 'text (cadr responses)) "שלום café κόσμος"))
      (should (= 0 (hash-table-count org-roam-retrieval-client--pending))))))

(ert-deftest org-roam-retrieval-client-errors-and-notifications ()
  (org-roam-retrieval-test-client
    (let (failure event done)
      (add-hook 'org-roam-retrieval-client-event-hook
                (lambda (method params) (setq event (cons method params))))
      (org-roam-retrieval-client-request "fail" nil #'ignore (lambda (value) (setq failure value)))
      (org-roam-retrieval-client-request "notify" nil (lambda (_) (setq done t)))
      (org-roam-retrieval-test-wait (lambda () (and failure done)))
      (should (= 42 (alist-get 'code failure)))
      (should (equal "progress" (car event)))
      (should (equal "ready" (alist-get 'state (cdr event)))))))

(ert-deftest org-roam-retrieval-client-cancellation-discards-late-results ()
  (org-roam-retrieval-test-client
    (let (callback)
      (let ((id (org-roam-retrieval-client-request "search" nil (lambda (_) (setq callback t)))))
        (org-roam-retrieval-client-cancel id))
      (org-roam-retrieval-test-wait (lambda () (eq org-roam-retrieval-client-state 'ready)))
      (accept-process-output nil 0.05)
      (should-not callback)
      (should (= 0 (hash-table-count org-roam-retrieval-client--pending))))))

(ert-deftest org-roam-retrieval-client-crash-and-restart ()
  (org-roam-retrieval-test-client
    (let (failure response)
      (org-roam-retrieval-client-request "crash" nil #'ignore (lambda (value) (setq failure value)))
      (org-roam-retrieval-test-wait (lambda () failure))
      (should (eq org-roam-retrieval-client-state 'failed))
      (org-roam-retrieval-client-request "search" nil (lambda (value) (setq response value)))
      (org-roam-retrieval-test-wait (lambda () response))
      (should (eq org-roam-retrieval-client-state 'ready)))))

(ert-deftest org-roam-retrieval-client-timeout-removes-request ()
  (org-roam-retrieval-test-client
    (let ((org-roam-retrieval-request-timeout 0.15) failure)
      (org-roam-retrieval-client-request "sleep" nil #'ignore (lambda (value) (setq failure value)))
      (org-roam-retrieval-test-wait (lambda () failure))
      (should (= -32002 (alist-get 'code failure)))
      (should (= 0 (hash-table-count org-roam-retrieval-client--pending))))))

(ert-deftest org-roam-retrieval-client-save-hooks-never-start-worker ()
  (org-roam-retrieval-test-client
    (org-roam-retrieval-client-enable)
    (org-roam-retrieval-client-enable)
    (should (= 1 (cl-count #'org-roam-retrieval-client--after-save after-save-hook)))
    (with-temp-buffer
      (setq buffer-file-name (expand-file-name "saved.org" directory))
      (cl-letf (((symbol-function 'org-roam-retrieval-client-start)
                 (lambda () (ert-fail "save started worker"))))
        (org-roam-retrieval-client--after-save)
        (org-roam-retrieval-client--flush-dirty))
      (should (gethash buffer-file-name org-roam-retrieval-client--dirty)))
    (org-roam-retrieval-client-disable)
    (should-not (memq #'org-roam-retrieval-client--after-save after-save-hook))))

(ert-deftest org-roam-retrieval-client-cancels-after-initial-response ()
  (org-roam-retrieval-test-client
    (let (initial status)
      (let ((id (org-roam-retrieval-client-request "search" nil (lambda (value) (setq initial value)))))
        (org-roam-retrieval-test-wait (lambda () initial))
        (should-not (gethash id org-roam-retrieval-client--pending))
        (org-roam-retrieval-client-cancel id)
        (org-roam-retrieval-client-request "status" nil (lambda (value) (setq status value)))
        (org-roam-retrieval-test-wait (lambda () status))
        (should (= id (alist-get 'cancelled status)))))))

(ert-deftest org-roam-retrieval-client-ui-handler-error-does-not-kill-worker ()
  (org-roam-retrieval-test-client
    (let (response)
      (add-hook 'org-roam-retrieval-client-event-hook
                (lambda (&rest _) (error "Simulated UI rendering failure")))
      (org-roam-retrieval-client-request "notify" nil (lambda (value) (setq response value)))
      (org-roam-retrieval-test-wait (lambda () response))
      (should (process-live-p org-roam-retrieval-client--process))
      (should (eq org-roam-retrieval-client-state 'ready)))))

(ert-deftest org-roam-retrieval-client-ocr-initialization-contract ()
  (let* ((org-roam-retrieval-ocr-enabled nil)
         (org-roam-retrieval-ocr-languages '("eng" "heb" "grc"))
         (org-roam-retrieval-ocr-data-directory "~/ocr-data")
         (ocr (alist-get 'ocr (org-roam-retrieval-client--initialize-params))))
    (should (eq :false (alist-get 'enabled ocr)))
    (should (equal ["eng" "heb" "grc"] (alist-get 'languages ocr)))
    (should (file-name-absolute-p (alist-get 'data_dir ocr)))))

(ert-deftest org-roam-retrieval-client-explicit-setup-provisions-runtime-and-model ()
  (org-roam-retrieval-test-client
    (let* ((uv (expand-file-name "uv" directory))
           (python (expand-file-name "model-python" directory))
           (org-roam-retrieval-python-program python)
           (org-roam-retrieval-client--setup-process nil)
           (find-executable (symbol-function 'executable-find))
           response failure)
      (with-temp-file uv (insert "#!/bin/sh\nexit 0\n"))
      (with-temp-file python
        (insert "#!/bin/sh\nprintf '%s\\n' '{\"cache_present\":true,\"download_requested\":false}'\n"))
      (set-file-modes uv #o700) (set-file-modes python #o700)
      (cl-letf (((symbol-function 'executable-find)
                 (lambda (name &optional remote)
                   (if (equal name "uv") uv (funcall find-executable name remote)))))
        (org-roam-retrieval-client-setup (lambda (value) (setq response value))
                                         (lambda (value) (setq failure value)))
        (org-roam-retrieval-test-wait (lambda () (or response failure))))
      (should-not failure)
      (should (eq t (alist-get 'cache_present (alist-get 'model response))))
      (should (eq :false (alist-get 'download_requested (alist-get 'model response)))))))

(ert-deftest org-roam-retrieval-client-model-download-is-explicit-setup-only ()
  (org-roam-retrieval-test-client
    (let* ((python (expand-file-name "model-python" directory))
           (org-roam-retrieval-python-program python)
           (org-roam-retrieval-client--setup-process nil)
           (org-roam-retrieval-setup-download-model t)
           response failure)
      (with-temp-file python
        (insert "#!/bin/sh\ncase \"$*\" in\n*--download*) printf '%s\\n' '{\"cache_present\":true,\"download_requested\":true}';;\n*) printf '%s\\n' '{\"cache_present\":false,\"error\":\"Not cached\"}'; exit 1;;\nesac\n"))
      (set-file-modes python #o700)
      (org-roam-retrieval-client--setup-model directory
                                             (lambda (value) (setq response value))
                                             (lambda (value) (setq failure value)))
      (org-roam-retrieval-test-wait (lambda () (or response failure)))
      (should-not failure)
      (should (eq t (alist-get 'download_requested (alist-get 'model response)))))))
