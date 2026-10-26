;;; org-roam-retrieval-client.el --- Managed retrieval worker client -*- lexical-binding: t; -*-

;; Copyright (C) 2026
;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:
;; Asynchronous JSON-RPC client and worker ownership for Org-roam retrieval.
;; Requiring this file starts no processes, scans, downloads, or timers.

;;; Code:
(require 'cl-lib)
(require 'json)
(require 'subr-x)

(defgroup org-roam-retrieval nil
  "Integrated local note and document retrieval."
  :group 'org-roam :prefix "org-roam-retrieval-")

(defconst org-roam-retrieval-client--package-directory
  (file-name-directory (directory-file-name
                        (file-name-directory (or load-file-name buffer-file-name)))))
(defcustom org-roam-retrieval-cache-directory
  (expand-file-name "org-roam-retrieval/" user-emacs-directory)
  "Directory for derived retrieval state and the managed Python runtime."
  :type 'directory :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-database-file nil
  "Retrieval SQLite path, or nil for index.sqlite inside the cache directory.
This database must be separate from Org-roam's graph database."
  :type '(choice (const nil) file) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-roots nil
  "Indexed roots, as alists containing symbol keys id and path."
  :type '(repeat alist) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-runtime-directory nil
  "Managed Python environment, or nil for venv inside the cache directory."
  :type '(choice (const nil) directory) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-python-program nil
  "Worker Python executable, or nil for the managed environment's Python."
  :type '(choice (const nil) file) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-model "google/embeddinggemma-2"
  "Embedding model identity.  Ordinary searches never download models."
  :type 'string :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-model-revision "914f7f89142e33e77833254d9c9b90c3cef7303b"
  "Immutable model revision selected during setup."
  :type 'string :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-device "cpu"
  "Embedding device, validated by the worker."
  :type '(choice (const "cpu") (const "mps"))
  :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-dimension 256
  "Embedding dimension; changing it selects a distinct embedding profile."
  :type 'integer :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-ocr-enabled t
  "Whether document extraction may use local OCR tools."
  :type 'boolean :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-ocr-languages '("eng" "heb" "grc")
  "Requested Tesseract language codes; missing packs are reported explicitly."
  :type '(repeat string) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-ocr-data-directory nil
  "Tesseract trained-data directory, or nil for the tool's default."
  :type '(choice (const nil) directory) :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-setup-download-model t
  "Whether explicit setup may fetch the pinned model if its offline check fails.
Ordinary worker startup and searching always remain offline."
  :type 'boolean :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-request-timeout 180
  "Seconds allowed for a worker response after it is sent."
  :type 'number :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-reconcile-delay 1.0
  "Idle seconds before sending coalesced saved-file invalidations."
  :type 'number :group 'org-roam-retrieval)
(defcustom org-roam-retrieval-emacs-program
  (expand-file-name invocation-name invocation-directory)
  "Background Emacs executable used to parse saved Org files."
  :type 'file :group 'org-roam-retrieval)

(defvar org-roam-retrieval-client-event-hook nil
  "Functions called with notification METHOD and PARAMS from the worker.")
(defvar org-roam-retrieval-client-state 'stopped
  "Current worker lifecycle state: stopped, starting, ready, or failed.")
(defvar org-roam-retrieval-client-last-error nil
  "Last worker error alist, or nil.")
(defvar org-roam-retrieval-client--process nil)
(defvar org-roam-retrieval-client--sequence 0)
(defvar org-roam-retrieval-client--pending (make-hash-table :test #'eql))
(defvar org-roam-retrieval-client--queue nil)
(defvar org-roam-retrieval-client--dirty (make-hash-table :test #'equal))
(defvar org-roam-retrieval-client--dirty-timer nil)
(defvar org-roam-retrieval-client--enabled nil)
(defvar org-roam-retrieval-client--setup-process nil)

(cl-defstruct (org-roam-retrieval-client--call
               (:constructor org-roam-retrieval-client--make-call))
  method params success error timer sent)

(defun org-roam-retrieval-client--runtime ()
  "Return the managed runtime directory."
  (expand-file-name (or org-roam-retrieval-runtime-directory
                        (expand-file-name "venv/" org-roam-retrieval-cache-directory))))

(defun org-roam-retrieval-client--python ()
  "Return the configured worker executable."
  (or org-roam-retrieval-python-program
      (expand-file-name "bin/python" (org-roam-retrieval-client--runtime))))

(defun org-roam-retrieval-client--invoke (callback value)
  "Call CALLBACK with VALUE without breaking process delivery."
  (when callback
    (condition-case err (funcall callback value)
      (error (message "Retrieval callback failed: %s" (error-message-string err))))))

(defun org-roam-retrieval-client--error (code message &optional data)
  "Build an error alist from CODE, MESSAGE and DATA."
  `((code . ,code) (message . ,message) (data . ,data)))

(defun org-roam-retrieval-client--finish (id success value)
  "Finish request ID with SUCCESS boolean and VALUE exactly once."
  (when-let* ((call (gethash id org-roam-retrieval-client--pending)))
    (remhash id org-roam-retrieval-client--pending)
    (when (timerp (org-roam-retrieval-client--call-timer call))
      (cancel-timer (org-roam-retrieval-client--call-timer call)))
    (org-roam-retrieval-client--invoke
     (if success (org-roam-retrieval-client--call-success call)
       (or (org-roam-retrieval-client--call-error call)
           (lambda (error) (message "Retrieval: %s" (alist-get 'message error))))) value)))

(defun org-roam-retrieval-client--fail-all (error)
  "Complete all outstanding requests with ERROR."
  (setq org-roam-retrieval-client-last-error error
        org-roam-retrieval-client--queue nil)
  (let (ids)
    (maphash (lambda (id _) (push id ids)) org-roam-retrieval-client--pending)
    (dolist (id ids) (org-roam-retrieval-client--finish id nil error))))

(defun org-roam-retrieval-client--write (message)
  "Write one UTF-8 JSON MESSAGE to the worker."
  (unless (process-live-p org-roam-retrieval-client--process)
    (error "Retrieval worker is not running"))
  (let ((json (decode-coding-string (json-serialize message :null-object nil) 'utf-8)))
    (when (> (string-bytes json) (* 4 1024 1024))
      (error "Retrieval request exceeds the 4 MiB message limit"))
    (process-send-string org-roam-retrieval-client--process (concat json "\n"))))

(defun org-roam-retrieval-client--timeout (id)
  "Fail an outstanding request ID on timeout."
  (when (gethash id org-roam-retrieval-client--pending)
    (org-roam-retrieval-client--finish
     id nil (org-roam-retrieval-client--error -32002 "Retrieval request timed out"))
    (when (process-live-p org-roam-retrieval-client--process)
      (ignore-errors
        (org-roam-retrieval-client--write
         `((jsonrpc . "2.0") (method . "cancel") (params . ((request_id . ,id)))))))))

(defun org-roam-retrieval-client--send (id)
  "Send queued request ID and start its response timeout."
  (when-let* ((call (gethash id org-roam-retrieval-client--pending)))
    (setf (org-roam-retrieval-client--call-sent call) t
          (org-roam-retrieval-client--call-timer call)
          (run-at-time org-roam-retrieval-request-timeout nil
                       #'org-roam-retrieval-client--timeout id))
    (condition-case err
        (org-roam-retrieval-client--write
         `((jsonrpc . "2.0") (id . ,id)
           (method . ,(org-roam-retrieval-client--call-method call))
           (params . ,(or (org-roam-retrieval-client--call-params call)
                           (make-hash-table)))))
      (error (org-roam-retrieval-client--finish
              id nil (org-roam-retrieval-client--error -32000 (error-message-string err)))))))

(defun org-roam-retrieval-client--dispatch (message)
  "Validate and dispatch one parsed JSON-RPC MESSAGE."
  (unless (equal (alist-get 'jsonrpc message) "2.0")
    (error "Worker returned an unsupported JSON-RPC version"))
  (if (assq 'id message)
      (let ((id (alist-get 'id message)))
        (unless (integerp id) (error "Worker response ID is not an integer"))
        (cond ((assq 'error message)
               (let ((failure (alist-get 'error message)))
                 (unless (and (listp failure) (integerp (alist-get 'code failure))
                              (stringp (alist-get 'message failure)))
                   (error "Worker returned an invalid JSON-RPC error object"))
                 (org-roam-retrieval-client--finish id nil failure)))
              ((assq 'result message)
               (org-roam-retrieval-client--finish id t (alist-get 'result message)))
              (t (error "Worker response contains neither result nor error"))))
    (when-let* ((method (alist-get 'method message)))
      (unless (stringp method) (error "Worker notification method is invalid"))
      (condition-case err
          (run-hook-with-args 'org-roam-retrieval-client-event-hook
                              method (alist-get 'params message))
        (error (message "Retrieval event handler failed: %s" (error-message-string err)))))))

(defun org-roam-retrieval-client--filter (process chunk)
  "Decode newline-delimited worker output CHUNK from PROCESS."
  (when (eq process org-roam-retrieval-client--process)
    (condition-case err
        (let ((pending (concat (or (process-get process 'partial) "") chunk)) line-end)
          (when (> (string-bytes pending) (* 4 1024 1024))
            (error "Worker output exceeds the 4 MiB message limit"))
          (while (setq line-end (string-search "\n" pending))
            (let ((line (substring pending 0 line-end)))
              (setq pending (substring pending (1+ line-end)))
              (unless (string-empty-p line)
                (org-roam-retrieval-client--dispatch
                 (json-parse-string line :object-type 'alist :array-type 'list
                                    :null-object nil :false-object :false)))))
          (process-put process 'partial pending))
      (error
       (setq org-roam-retrieval-client-state 'failed)
       (org-roam-retrieval-client--fail-all
        (org-roam-retrieval-client--error -32700 (error-message-string err)))
       (delete-process process)))))

(defun org-roam-retrieval-client--sentinel (process event)
  "Handle worker PROCESS termination EVENT."
  (when (and (eq process org-roam-retrieval-client--process)
             (memq (process-status process) '(exit signal failed)))
    (setq org-roam-retrieval-client--process nil
          org-roam-retrieval-client-state 'failed)
    (let ((detail (when-let* ((buffer (process-get process 'stderr-buffer)))
                    (when (buffer-live-p buffer)
                      (with-current-buffer buffer
                        (string-trim (buffer-substring-no-properties
                                      (max (point-min) (- (point-max) 2000)) (point-max))))))))
      (org-roam-retrieval-client--fail-all
       (org-roam-retrieval-client--error
        -32000 (concat "Retrieval worker stopped: " (string-trim event)
                       (unless (string-empty-p (or detail "")) (concat "; " detail))))))))

(defun org-roam-retrieval-client--initialize-params ()
  "Return the worker's explicit configuration."
  `((database . ,(expand-file-name
                  (or org-roam-retrieval-database-file
                      (expand-file-name "index.sqlite" org-roam-retrieval-cache-directory))))
    (roots . ,(vconcat
               (mapcar (lambda (root)
                         (let ((copy (copy-tree root)))
                           (setf (alist-get 'path copy)
                                 (expand-file-name (alist-get 'path copy)))
                           copy)) org-roam-retrieval-roots)))
    (ocr . ((enabled . ,(if org-roam-retrieval-ocr-enabled t :false))
            (languages . ,(vconcat org-roam-retrieval-ocr-languages))
            (data_dir . ,(when org-roam-retrieval-ocr-data-directory
                           (expand-file-name org-roam-retrieval-ocr-data-directory)))))
    (embedding . ((model . ,org-roam-retrieval-model)
                  (revision . ,org-roam-retrieval-model-revision)
                  (device . ,org-roam-retrieval-device)
                  (dimension . ,org-roam-retrieval-dimension)))
    (emacs . ,org-roam-retrieval-emacs-program)
    (extractor . ,(expand-file-name "extensions/org-roam-retrieval-extract.el"
                                   org-roam-retrieval-client--package-directory))))

(defun org-roam-retrieval-client-start ()
  "Start the worker and asynchronously initialize it once.
No model or dependency download occurs.  Use setup explicitly beforehand."
  (unless (process-live-p org-roam-retrieval-client--process)
    (let ((python (org-roam-retrieval-client--python))
          (default-directory (expand-file-name "python/" org-roam-retrieval-client--package-directory))
          (stderr (get-buffer-create " *org-roam-retrieval-stderr*")))
      (unless (file-executable-p python)
        (error "Retrieval runtime is missing; run M-x org-roam-retrieval-setup"))
      (with-current-buffer stderr (erase-buffer))
      (setq org-roam-retrieval-client-state 'starting
            org-roam-retrieval-client-last-error nil
            org-roam-retrieval-client--process
            (make-process :name "org-roam-retrieval" :command (list python "-m" "org_roam_retrieval")
                          :connection-type 'pipe :coding 'utf-8-unix :noquery t
                          :stderr stderr :filter #'org-roam-retrieval-client--filter
                          :sentinel #'org-roam-retrieval-client--sentinel))
      (process-put org-roam-retrieval-client--process 'stderr-buffer stderr)
      (let ((id (cl-incf org-roam-retrieval-client--sequence)))
        (puthash id
                 (org-roam-retrieval-client--make-call
                  :method "initialize" :params (org-roam-retrieval-client--initialize-params)
                  :success (lambda (_result)
                             (setq org-roam-retrieval-client-state 'ready)
                             (let ((queue org-roam-retrieval-client--queue))
                               (setq org-roam-retrieval-client--queue nil)
                               (dolist (request queue) (org-roam-retrieval-client--send request))))
                  :error (lambda (error)
                           (setq org-roam-retrieval-client-state 'failed)
                           (org-roam-retrieval-client--fail-all error)
                           (when (process-live-p org-roam-retrieval-client--process)
                             (delete-process org-roam-retrieval-client--process))))
                 org-roam-retrieval-client--pending)
        (org-roam-retrieval-client--send id))))
  org-roam-retrieval-client--process)

(defun org-roam-retrieval-client-request (method params success &optional error)
  "Send asynchronous METHOD with PARAMS, invoking SUCCESS or ERROR.
Return an integer request ID.  Objects use symbol-keyed alists and arrays
use vectors when encoded; returned arrays are lists.  Requests queue until
the worker completes initialization."
  (unless (stringp method) (error "Retrieval method must be a string"))
  (let ((id (cl-incf org-roam-retrieval-client--sequence)))
    (puthash id (org-roam-retrieval-client--make-call
                 :method method :params params :success success :error error)
             org-roam-retrieval-client--pending)
    (condition-case err
        (progn
          (if (and (process-live-p org-roam-retrieval-client--process)
                   (eq org-roam-retrieval-client-state 'ready))
              (org-roam-retrieval-client--send id)
            (setq org-roam-retrieval-client--queue
                  (nconc org-roam-retrieval-client--queue (list id)))
            (org-roam-retrieval-client-start)))
      (error (org-roam-retrieval-client--fail-all
              (org-roam-retrieval-client--error -32000 (error-message-string err)))))
    id))

(defun org-roam-retrieval-client-cancel (id)
  "Cancel ID locally and notify the worker, including deferred reranking.
A search may already have returned lexical results while its semantic stage
is still running, so cancellation is sent even after the initial callback."
  (when-let* ((call (gethash id org-roam-retrieval-client--pending)))
    (remhash id org-roam-retrieval-client--pending)
    (setq org-roam-retrieval-client--queue (delq id org-roam-retrieval-client--queue))
    (when (timerp (org-roam-retrieval-client--call-timer call))
      (cancel-timer (org-roam-retrieval-client--call-timer call))))
  (when (process-live-p org-roam-retrieval-client--process)
    (ignore-errors
      (org-roam-retrieval-client--write
       `((jsonrpc . "2.0") (method . "cancel") (params . ((request_id . ,id))))))))

(defun org-roam-retrieval-client-stop ()
  "Stop the owned worker and fail pending requests; authored data is untouched."
  (interactive)
  (let ((process org-roam-retrieval-client--process))
    (setq org-roam-retrieval-client--process nil
          org-roam-retrieval-client-state 'stopped)
    (when (process-live-p process) (delete-process process)))
  (org-roam-retrieval-client--fail-all
   (org-roam-retrieval-client--error -32800 "Retrieval worker stopped by user")))

(defun org-roam-retrieval-client--setup-model (runtime success error &optional download)
  "Check the pinned model for RUNTIME, calling SUCCESS or ERROR.
DOWNLOAD permits network access only in this explicit setup subprocess."
  (let ((output (generate-new-buffer " *org-roam-model-setup*"))
        (diagnostics (get-buffer-create "*Org-roam retrieval setup*")))
    (with-current-buffer diagnostics
      (goto-char (point-max))
      (insert (if download "Fetching pinned model snapshot…\n" "Checking pinned model cache offline…\n")))
    (setq org-roam-retrieval-client--setup-process
          (make-process
           :name "org-roam-model-setup" :buffer output :stderr diagnostics
           :coding 'utf-8-unix :connection-type 'pipe :noquery t
           :command (append (list (org-roam-retrieval-client--python) "-m"
                                  "org_roam_retrieval.model_setup" "--revision"
                                  org-roam-retrieval-model-revision)
                            (when download (list "--download")))
           :sentinel
           (lambda (process _event)
             (when (memq (process-status process) '(exit signal failed))
               (setq org-roam-retrieval-client--setup-process nil)
               (unwind-protect
                   (let ((model (condition-case nil
                                    (with-current-buffer output
                                      (json-parse-string (buffer-string) :object-type 'alist
                                                         :null-object nil :false-object :false))
                                  (error nil))))
                     (cond
                      ((and (eq (process-status process) 'exit)
                            (zerop (process-exit-status process))
                            (eq t (alist-get 'cache_present model)))
                       (with-current-buffer diagnostics (goto-char (point-max))
                                            (insert "Pinned embedding model is ready.\n"))
                       (org-roam-retrieval-client--invoke
                        success `((runtime . ,runtime) (model . ,model))))
                      ((and (not download) org-roam-retrieval-setup-download-model
                            (eq (process-status process) 'exit))
                       (org-roam-retrieval-client--setup-model runtime success error t))
                      (t
                       (with-current-buffer diagnostics
                         (goto-char (point-max))
                         (insert (or (alist-get 'error model)
                                     "Model setup did not return a valid readiness response.") "\n"))
                       (org-roam-retrieval-client--invoke
                        error (org-roam-retrieval-client--error
                               -32000 "Pinned model is unavailable; inspect *Org-roam retrieval setup*. Check cache/access or allow model download during explicit setup.")))))
                 (kill-buffer output))))))))

(defun org-roam-retrieval-client-setup (success &optional error)
  "Provision the pinned runtime and model asynchronously.
Check the model cache offline first.  Only this explicit setup operation may
fetch the pinned model when `org-roam-retrieval-setup-download-model' permits.
Call SUCCESS with runtime/model readiness, or ERROR with an error alist."
  (when (process-live-p org-roam-retrieval-client--setup-process)
    (user-error "Retrieval setup is already running"))
  (if-let* ((uv (executable-find "uv")))
      (let* ((runtime (org-roam-retrieval-client--runtime))
             (project (expand-file-name "python/" org-roam-retrieval-client--package-directory))
             (process-environment (cons (concat "UV_PROJECT_ENVIRONMENT=" runtime) process-environment))
             (buffer (get-buffer-create "*Org-roam retrieval setup*")))
        (org-roam-retrieval-client-stop)
        (make-directory org-roam-retrieval-cache-directory t)
        (with-current-buffer buffer (let ((inhibit-read-only t)) (erase-buffer)))
        (setq org-roam-retrieval-client--setup-process
              (make-process
               :name "org-roam-retrieval-setup" :buffer buffer :noquery t
               :coding 'utf-8-unix :connection-type 'pipe
               :command (list uv "sync" "--project" project "--frozen" "--python" "3.12" "--extra" "embedding")
               :sentinel
               (lambda (process _event)
                 (when (memq (process-status process) '(exit signal failed))
                   (setq org-roam-retrieval-client--setup-process nil)
                   (if (and (eq (process-status process) 'exit)
                            (zerop (process-exit-status process)))
                       (org-roam-retrieval-client--setup-model runtime success error)
                     (org-roam-retrieval-client--invoke
                      error (org-roam-retrieval-client--error
                             -32000 "Runtime setup failed; inspect *Org-roam retrieval setup*"))))))))
    (org-roam-retrieval-client--invoke
     error (org-roam-retrieval-client--error -32000 "Install uv to provision the managed retrieval runtime"))))

(defun org-roam-retrieval-client--flush-dirty ()
  "Send coalesced saved paths when the worker is already available."
  (setq org-roam-retrieval-client--dirty-timer nil)
  (when (and (> (hash-table-count org-roam-retrieval-client--dirty) 0)
             (process-live-p org-roam-retrieval-client--process))
    (let ((paths (hash-table-keys org-roam-retrieval-client--dirty)))
      (clrhash org-roam-retrieval-client--dirty)
      (org-roam-retrieval-client-request
       "reconcile" `((paths . ,(vconcat paths))) #'ignore
       (lambda (_error)
         (dolist (path paths) (puthash path t org-roam-retrieval-client--dirty)))))))

(defun org-roam-retrieval-client--after-save ()
  "Record a cheap invalidation; never start or wait for a worker while saving."
  (when (and buffer-file-name (not (file-remote-p buffer-file-name)))
    (puthash buffer-file-name t org-roam-retrieval-client--dirty)
    (when (timerp org-roam-retrieval-client--dirty-timer)
      (cancel-timer org-roam-retrieval-client--dirty-timer))
    (setq org-roam-retrieval-client--dirty-timer
          (run-with-idle-timer org-roam-retrieval-reconcile-delay nil
                               #'org-roam-retrieval-client--flush-dirty))))

(defun org-roam-retrieval-client-enable ()
  "Enable lightweight saved-file invalidation without starting the worker."
  (unless org-roam-retrieval-client--enabled
    (add-hook 'after-save-hook #'org-roam-retrieval-client--after-save)
    (add-hook 'kill-emacs-hook #'org-roam-retrieval-client-stop)
    (setq org-roam-retrieval-client--enabled t)))

(defun org-roam-retrieval-client-disable ()
  "Remove invalidation hooks and stop the worker."
  (interactive)
  (remove-hook 'after-save-hook #'org-roam-retrieval-client--after-save)
  (remove-hook 'kill-emacs-hook #'org-roam-retrieval-client-stop)
  (when (timerp org-roam-retrieval-client--dirty-timer)
    (cancel-timer org-roam-retrieval-client--dirty-timer))
  (setq org-roam-retrieval-client--dirty-timer nil
        org-roam-retrieval-client--enabled nil)
  (org-roam-retrieval-client-stop))

(provide 'org-roam-retrieval-client)
;;; org-roam-retrieval-client.el ends here
