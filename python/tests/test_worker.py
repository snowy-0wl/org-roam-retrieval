"""Behavioral contracts for the managed retrieval worker."""
from __future__ import annotations

import io
import json
from pathlib import Path
import queue
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest

import numpy as np

from org_roam_retrieval.embedding import Profile, validate_vectors
from org_roam_retrieval.extract import chunks, document, prepare_chunks
from org_roam_retrieval.store import Store
from org_roam_retrieval.worker import Service


class FakeEncoder:
    def __init__(self, profile):
        self.profile = profile
        self.ready = True
        self.calls = 0
    def _vector(self, text):
        vector = np.zeros(self.profile.dimension, dtype=np.float32)
        for index, words in enumerate((('galaxy', 'stars', 'astronomy', 'cosmos', 'κόσμος'), ('bread', 'baking', 'flour'), ('river', 'water', 'נהר'))):
            vector[index] = sum(text.lower().count(word) for word in words)
        vector[3] = .01
        return validate_vectors(vector, self.profile.dimension)[0]
    def documents(self, texts):
        self.calls += 1
        return np.array([self._vector(text) for text in texts])
    def query(self, text):
        return self._vector(text)


def drain(service):
    count = 0
    deadline = time.monotonic() + 15
    while not service.tasks.empty() or service.model_busy or service.model_queue or service.extraction_busy or service.extraction_queue:
        if time.monotonic() > deadline:
            raise AssertionError("Worker did not drain within deadline")
        try:
            _, _, function, args = service.tasks.get(timeout=.02)
        except queue.Empty:
            continue
        function(*args)
        count += 1
        if count > 1000:
            raise AssertionError("Background work did not converge")


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="roam corpus ' ")
        self.root = Path(self.temporary.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.messages = []
        self.service = Service(self.messages.append, FakeEncoder)
        self.service.rpc_initialize({"database": str(self.root / 'index.sqlite'),
                                     "roots": [{"id": "notes", "path": str(self.notes)},
                                               {"id": "missing", "path": str(self.root / 'missing')}],
                                     "embedding": {"dimension": 16},
                                     "emacs": "emacs",
                                     "extractor": str(Path(__file__).resolve().parents[2] / 'extensions/org-roam-retrieval-extract.el')})
        drain(self.service)
    def tearDown(self):
        self.service.extraction_executor.shutdown(wait=True, cancel_futures=True)
        self.service.model_executor.shutdown(wait=True, cancel_futures=True)
        self.service.store.close()
        self.temporary.cleanup()
    def write(self, name, text):
        path = self.notes / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return str(path.resolve())
    def refresh(self, **params):
        result = self.service.rpc_reconcile(params)
        drain(self.service)
        return result
    def search(self, text, **params):
        response = self.service.rpc_search({"text": text, **params})
        if response.get('semantic_pending'):
            drain(self.service)
            response = self.service.rpc_search({"text": text, **params})
        return response

    def test_incremental_change_delete_and_noop_embedding_reuse(self):
        path = self.write('space.txt', 'galaxy stars astronomy')
        self.refresh()
        self.assertEqual(1, self.search('galaxy')['coverage']['sources'])
        calls = self.service.encoder.calls
        generation = self.service.store.generation
        self.refresh()
        self.assertEqual(generation, self.service.store.generation)
        self.assertEqual(calls, self.service.encoder.calls)
        self.write('space.txt', 'bread flour baking')
        self.refresh(paths=[path])
        self.assertFalse(self.search('galaxy', mode='lexical')['results'])
        self.assertTrue(self.search('bread')['results'])
        Path(path).unlink()
        self.refresh(paths=[path])
        self.assertFalse(self.search('bread')['results'])

    def test_semantic_candidates_do_not_require_lexical_match(self):
        space = self.write('space.txt', 'galaxy stars astronomy')
        self.write('food.txt', 'bread flour baking')
        self.refresh()
        self.assertFalse(self.search('cosmos', mode='lexical')['results'])
        result = self.search('cosmos')['results'][0]
        self.assertEqual(space, result['path'])
        self.assertEqual({'semantic': 1}, result['ranks'])

    def test_unicode_literal_fts_and_missing_root(self):
        self.write('שלום.txt', 'café κόσμος שלום נהר')
        self.refresh()
        for query in ('café', 'cafe', 'κόσμος', 'שלום', '" OR * café : - ('):
            self.assertTrue(self.search(query, mode='lexical')['results'], query)
        self.assertFalse(self.search('* : "', mode='lexical')['results'])
        self.assertEqual(['missing'], self.service.rpc_status({})['unavailable_roots'])

    def test_file_scope_excludes_only_actual_source(self):
        self_path = self.write('self.txt', 'galaxy stars astronomy')
        other = self.write('other.txt', 'galaxy stars astronomy telescope')
        self.refresh()
        results = self.search('galaxy', source={'path': self_path}, scope='file')['results']
        self.assertEqual([other], [result['path'] for result in results])
        self.assertEqual(2, len(self.search('galaxy', scope='query')['results']))

    def test_many_passages_in_one_book_do_not_crowd_out_other_sources(self):
        self.write('book.txt', 'galaxy stars astronomy ' * 1000)
        other = self.write('short.txt', 'galaxy astronomy')
        self.refresh()
        self.assertGreater(self.service.store.coverage()['chunks'],10)
        self.assertEqual(2,len(self.search('galaxy')['results']))
        self.assertIn(other,[result['path'] for result in self.search('galaxy')['results']])

    def test_root_filter_applies_before_top_candidates(self):
        self.write('space.txt', 'galaxy astronomy')
        self.refresh()
        self.assertFalse(self.search('galaxy',root_ids=['missing'])['results'])
        self.assertFalse(self.search('galaxy',root_ids=[])['results'])
        self.assertTrue(self.search('galaxy',root_ids=['notes'])['results'])

    def test_hidden_and_symlink_paths_are_excluded(self):
        self.write('.hidden/private.txt', 'galaxy private')
        target = self.write('visible.txt', 'galaxy public')
        (self.notes / 'link.txt').symlink_to(target)
        (self.notes / 'loop').symlink_to(self.notes, target_is_directory=True)
        self.refresh()
        self.assertEqual(1, self.service.store.coverage()['sources'])

    def test_filename_only_is_explicit_not_semantic_content(self):
        self.write('galaxy.png', 'not an image')
        self.refresh()
        result = self.search('galaxy', mode='lexical')['results'][0]
        self.assertEqual('filename-only', result['coverage'])
        self.assertEqual(0, self.service.store.coverage()['embedded_chunks'])

    def test_repair_preview_preserves_files_and_cached_embeddings(self):
        path = self.write('space.txt', 'galaxy stars')
        self.refresh()
        before = self.service.encoder.calls
        self.service.rpc_repair({'action': 'rebuild', 'dry_run': True})
        self.assertTrue(self.search('galaxy')['results'])
        self.service.rpc_repair({'action': 'rebuild', 'dry_run': False})
        drain(self.service)
        self.assertTrue(Path(path).exists())
        self.assertTrue(self.search('galaxy')['results'])
        self.assertEqual(before, self.service.encoder.calls)

    def test_cancelled_request_does_not_run_or_return_success(self):
        self.service.accept({'jsonrpc': '2.0', 'id': 42, 'method': 'search', 'params': {'text': 'galaxy'}})
        self.service.accept({'jsonrpc': '2.0', 'method': 'cancel', 'params': {'request_id': 42}})
        drain(self.service)
        reply = next(message for message in self.messages if message.get('id') == 42)
        self.assertEqual(-32800, reply['error']['code'])

    def test_org_helper_preserves_file_heading_ids_and_locations(self):
        path = self.write('note.org', ':PROPERTIES:\n:ID: file-id\n:END:\n#+title: Café שלום\nFile prose stars\n* Heading\n:PROPERTIES:\n:ID: heading-id\n:END:\nGalaxy astronomy\n#+begin_src emacs-lisp\n(error "never execute")\n#+end_src\n')
        self.refresh()
        results = self.search('galaxy', mode='lexical')['results']
        self.assertTrue(results, self.service.rpc_status({}))
        self.assertEqual('heading-id', results[0]['locator']['id'])
        self.assertEqual(['Heading'], results[0]['locator']['outline'])
        self.assertFalse(self.search('never execute', mode='lexical')['results'])
        self.assertEqual(path, results[0]['path'])

    def test_lexical_and_status_do_not_wait_for_slow_model(self):
        self.write('slow.txt', 'galaxy astronomy')
        blocked = threading.Event()
        release = threading.Event()
        original = self.service.encoder.documents
        def slow(texts):
            blocked.set()
            release.wait(5)
            return original(texts)
        self.service.encoder.documents = slow
        self.service.rpc_reconcile({})
        while not blocked.is_set():
            _, _, function, args = self.service.tasks.get(timeout=2)
            function(*args)
        started = time.perf_counter()
        response = self.service.rpc_search({'text':'galaxy','mode':'lexical'})
        status = self.service.rpc_status({})
        elapsed = time.perf_counter()-started
        self.assertTrue(response['results'])
        self.assertGreater(status['backlog'],0)
        self.assertLess(elapsed,.15)
        release.set()
        drain(self.service)

    def test_deleted_and_modified_results_are_not_reported_current(self):
        path = self.write('change.txt', 'galaxy astronomy')
        self.refresh()
        self.write('change.txt', 'galaxy telescope different content')
        response = self.service.rpc_search({'text':'galaxy','mode':'lexical'})
        self.assertTrue(response['results'][0]['stale'])
        drain(self.service)
        self.assertFalse(self.search('galaxy',mode='lexical')['results'][0]['stale'])
        Path(path).unlink()
        self.assertFalse(self.service.rpc_search({'text':'galaxy','mode':'lexical'})['results'])

    def test_lexical_and_status_do_not_wait_for_slow_extractor(self):
        from unittest.mock import patch
        self.write('first.txt', 'galaxy stars')
        self.refresh()
        self.write('slow.txt', 'galaxy astronomy')
        blocked,release=threading.Event(),threading.Event()
        from org_roam_retrieval.worker import document as original
        def slow(path, options=None):
            blocked.set()
            release.wait(5)
            return original(path)
        with patch('org_roam_retrieval.worker.document',slow):
            self.service.rpc_reconcile({})
            while not blocked.wait(.005):
                try:
                    _,_,function,args=self.service.tasks.get(timeout=.01)
                    function(*args)
                except queue.Empty:
                    continue
            started=time.perf_counter()
            response=self.service.rpc_search({'text':'galaxy','mode':'lexical'})
            status=self.service.rpc_status({})
            self.assertTrue(response['results'])
            self.assertGreater(status['backlog'],0)
            self.assertLess(time.perf_counter()-started,.15)
            release.set()
            drain(self.service)

    def test_embeddings_start_before_slow_library_extraction_finishes(self):
        from unittest.mock import patch
        from org_roam_retrieval.worker import document as original
        self.write('a.txt','galaxy astronomy')
        self.write('b.txt','bread baking')
        blocked,release=threading.Event(),threading.Event()
        def extract(path,options=None):
            if Path(path).name=='b.txt':
                blocked.set()
                release.wait(5)
            return original(path,options)
        with patch('org_roam_retrieval.worker.document',extract):
            self.service.rpc_reconcile({})
            deadline=time.monotonic()+3
            try:
                while self.service.store.coverage()['embedded_chunks']==0 and time.monotonic()<deadline:
                    try:
                        _,_,function,args=self.service.tasks.get(timeout=.01)
                        function(*args)
                    except queue.Empty:
                        continue
                self.assertTrue(blocked.is_set())
                self.assertEqual(1,self.service.store.coverage()['embedded_chunks'])
                self.assertTrue(any(job['state']=='running' for job in self.service.jobs.values()))
            finally:
                release.set()
            drain(self.service)

    def test_reconcile_deduplicates_inflight_revisions(self):
        self.write('pending.txt','galaxy astronomy')
        first=self.service.rpc_reconcile({})['job_id']
        # Scan only, leaving the source pending in the extraction queue.
        _,_,function,args=self.service.tasks.get_nowait()
        function(*args)
        self.assertEqual(1,self.service.jobs[first]['total'])
        second=self.service.rpc_reconcile({})['job_id']
        # Locate/run the second scan while first extraction stays pending.
        self.service.scan(second,None,None)
        self.assertEqual(0,self.service.jobs[second]['total'])
        drain(self.service)
        self.assertEqual(1,self.service.store.coverage()['sources'])

    def test_invalid_rpc_and_unknown_method(self):
        self.service.accept({'jsonrpc': '2.0', 'id': 1, 'method': 'missing'})
        self.service.accept({'jsonrpc': '2.0', 'id': 2, 'method': 'search', 'params': ['invalid']})
        drain(self.service)
        errors = [message['error']['code'] for message in self.messages if 'error' in message]
        self.assertIn(-32601, errors)
        self.assertIn(-32602, errors)

    def test_bad_source_does_not_abort_other_sources(self):
        self.write('bad.pdf', 'not a PDF')
        self.write('good.txt', 'galaxy astronomy')
        self.refresh()
        self.assertTrue(self.search('galaxy')['results'])
        status = self.service.rpc_status({})
        self.assertEqual(1, status['coverage']['by_state']['error'])
        self.assertEqual(1, len(status['errors']))

    def test_restart_reuses_committed_index_and_embeddings(self):
        self.write('good.txt', 'galaxy astronomy')
        self.refresh()
        generation = self.service.store.generation
        self.service.store.close()
        self.service.store = Store(str(self.root / 'index.sqlite'), Profile(dimension=16))
        self.assertEqual(generation, self.service.store.generation)
        self.assertTrue(self.search('cosmos')['results'])


class NumericContracts(unittest.TestCase):
    def test_vectors_reject_mismatch_nan_and_zero(self):
        for value in ([1, 2], [float('nan')] * 16, [0] * 16):
            with self.assertRaises(ValueError):
                validate_vectors(value, 16)
        value = validate_vectors([3] * 16, 16)[0]
        self.assertAlmostEqual(1, np.linalg.norm(value))
    def test_profile_rejects_floating_revision_or_unsafe_dtype(self):
        with self.assertRaises(ValueError):
            Profile(revision='main')
        with self.assertRaises(ValueError):
            Profile(dtype='float16')
    def test_chunking_covers_tail_and_has_no_whole_file_cap(self):
        text = 'start ' + 'middle ' * 1000 + 'unique-tail'
        parts = list(chunks(text))
        self.assertGreater(len(parts), 2)
        self.assertTrue(parts[-1].endswith('unique-tail'))
        self.assertTrue(all(len(part) <= 1600 for part in parts))
    def test_corrupt_stored_vector_is_not_silently_shortened(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root + '/index.sqlite', Profile(dimension=16))
            source = {'path': root + '/x.txt', 'title': 'x', 'segments': [{'text': 'galaxy', 'locator': {'kind': 'file'}}]}
            store.upsert(source, 'root', '1', 'revision')
            rows = store.missing_embeddings()
            store.embed(rows, [[1] * 16])
            store.db.execute('UPDATE embeddings SET dimension=2')
            store.db.commit()
            with self.assertRaises(ValueError):
                store.vector_candidates([1] * 16)
            store.close()


class DocumentCoverage(unittest.TestCase):
    def test_docx_paragraphs_are_text_with_truthful_file_locator(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'note.docx'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>galaxy שלום</w:t></w:r></w:p></w:body></w:document>')
            result=document(str(path))
            self.assertEqual('file',result['segments'][0]['locator']['kind'])
            self.assertIn('galaxy שלום',result['segments'][0]['text'])


    @unittest.skipUnless(shutil.which('tesseract'), 'Optional Tesseract is not installed')
    def test_image_ocr_is_searchable_text_with_explicit_language(self):
        from PIL import Image,ImageDraw,ImageFont
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'scan.png'
            image=Image.new('RGB',(1600,300),'white')
            font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',90)
            ImageDraw.Draw(image).text((50,70),'GALAXY OBSERVATORY',font=font,fill='black')
            image.save(path)
            result=document(str(path))
            self.assertEqual('ocr-text',result['coverage'])
            self.assertIn('GALAXY',result['segments'][0]['text'])
            self.assertIn('eng',result['details']['ocr_languages'])
            self.assertFalse(result['details']['visual_embeddings'])


    def test_pdf_reads_all_pages_and_identifies_empty_pages(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'book.pdf'
            writer = PdfWriter()
            for number, text in enumerate(('galaxy firstpage', 'astronomy lastpage')):
                page = writer.add_blank_page(width=600, height=800)
                font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
                page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
                stream = DecodedStreamObject()
                stream.set_data(f'BT /F1 12 Tf 10 700 Td ({text}) Tj ET'.encode())
                page[NameObject('/Contents')] = writer._add_object(stream)
            writer.add_blank_page(width=600, height=800)
            writer.write(path)
            result = document(str(path))
            self.assertEqual('partial-text', result['coverage'])
            self.assertEqual([1, 2], [segment['locator']['page'] for segment in result['segments']])
            self.assertIn('lastpage', result['segments'][-1]['text'])
            self.assertEqual([3], result['details']['empty_pages'])
    def test_pdf_empty_open_password_is_readable_but_real_password_is_required(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with tempfile.TemporaryDirectory() as directory:
            for password in ("", "required-secret"):
                path = Path(directory) / ("open.pdf" if not password else "locked.pdf")
                writer = PdfWriter()
                page = writer.add_blank_page(width=600, height=800)
                font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
                page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
                stream = DecodedStreamObject()
                stream.set_data(b'BT /F1 12 Tf 10 700 Td (galaxy readable) Tj ET')
                page[NameObject('/Contents')] = writer._add_object(stream)
                writer.encrypt(user_password=password, owner_password="owner-secret")
                writer.write(path)
                before = path.read_bytes()
                if password:
                    with self.assertRaisesRegex(ValueError, "Password-protected PDF"):
                        document(str(path))
                else:
                    self.assertIn("galaxy readable", document(str(path))['segments'][0]['text'])
                self.assertEqual(before, path.read_bytes())

    def test_xlsx_keeps_sheet_and_cell_range(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'table.xlsx'
            workbook = Workbook()
            workbook.active.title = 'Greek'
            workbook.active.append(['κόσμος', 'galaxy'])
            workbook.save(path)
            workbook.close()
            result = document(str(path))
            self.assertEqual('Greek', result['segments'][0]['locator']['sheet'])
            self.assertIn('κόσμος', result['segments'][0]['text'])


if __name__ == '__main__':
    unittest.main()
