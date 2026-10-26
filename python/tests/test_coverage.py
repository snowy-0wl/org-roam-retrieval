"""Source fidelity and additive coverage-policy migration checks."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from org_roam_retrieval.embedding import Profile
from org_roam_retrieval.extract import document, org_batch, prepare_chunks
from org_roam_retrieval.formats import epub_document, safe_member
from org_roam_retrieval.policy import exclusion_reason, extraction_policy
from org_roam_retrieval.store import Store
import test_worker as worker_tests

FIXTURES = Path(__file__).parent / 'fixtures' / 'coverage'


def make_epub(path, *, first_href='Text/two.xhtml', second_href='Text/one.xhtml', extra=None):
    container='''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>'''
    opf=f'''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>A Unicode κόσμος book</dc:title></metadata><manifest><item id="one" href="{second_href}" media-type="application/xhtml+xml"/><item id="two" href="{first_href}" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="two"/><itemref idref="one"/></spine></package>'''
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('META-INF/container.xml',container)
        archive.writestr('OEBPS/book.opf',opf)
        archive.writestr('OEBPS/Text/one.xhtml','<html><head><title>One</title></head><body><h1 id="one">Chapter One</h1><p>First galaxy chapter.</p></body></html>')
        archive.writestr('OEBPS/Text/two.xhtml','<html><head><title>Two</title></head><body><h1 id="two">Chapter Two</h1><nav>navnoise</nav><p>Second reading order שלום.</p><script>scriptnoise</script></body></html>')
        archive.writestr('OEBPS/unused.xhtml','unreferencednoise')
        for name,value in (extra or {}).items():
            archive.writestr(name,value)


class FormatFixtures(unittest.TestCase):
    def test_html_visible_prose_title_unicode_and_no_executable_noise(self):
        result=document(str(FIXTURES/'prose.html'))
        self.assertEqual('Galaxy κόσμος שלום',result['title'])
        text=result['segments'][0]['text']
        self.assertIn('Visible galaxy prose & café שלום.',text)
        for noise in ('scriptnoise','stylenoise','navigationnoise','rolenoise','hiddennoise','displaynoise','footernoise'):
            self.assertNotIn(noise,text)
        self.assertEqual({'kind':'file'},result['segments'][0]['locator'])

    def test_latex_math_source_retained_without_commands_or_comments(self):
        result=document(str(FIXTURES/'formula.tex'))
        text=result['segments'][0]['text']
        self.assertIn('E=mc^2',text)
        self.assertIn(r'L=4\pi r^2\sigma T^4',text)
        self.assertIn('κόσμος',text)
        self.assertNotIn('commentnoise',text)
        self.assertNotIn('never-read',text)
        self.assertNotIn('never-execute',text)
        self.assertIn(r'\%',text)
        first=prepare_chunks(result)[0]
        original=(FIXTURES/'formula.tex').read_text().splitlines()
        self.assertIn('Stellar luminosity',original[first['locator']['line']-1])
        self.assertFalse(result['details']['commands_executed'])

    def test_bibtex_balanced_entries_retain_keys_and_fields(self):
        result=document(str(FIXTURES/'references.bib'))
        self.assertEqual(2,len(result['segments']))
        self.assertEqual('galaxy2026',result['segments'][0]['citation_key'])
        self.assertIn('Stellar',result['segments'][0]['text'])
        self.assertIn('שלום',result['segments'][0]['text'])
        self.assertEqual('text-range',result['segments'][1]['locator']['kind'])

    def test_epub_spine_order_and_real_member_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'book.epub'
            make_epub(path)
            result=document(str(path))
            segments=result['segments']
            self.assertEqual([0,1],[segment['locator']['spine_index'] for segment in segments])
            self.assertEqual(['OEBPS/Text/two.xhtml','OEBPS/Text/one.xhtml'],[segment['locator']['href'] for segment in segments])
            self.assertEqual('two',segments[0]['locator']['fragment'])
            self.assertIn('שלום',segments[0]['text'])
            self.assertNotIn('unreferencednoise',str(result))
            self.assertNotIn('scriptnoise',str(result))
            self.assertNotIn('navnoise',str(result))
            with zipfile.ZipFile(path) as archive:
                for segment in segments:
                    self.assertIn(segment['locator']['href'],archive.namelist())

    def test_epub_rejects_traversal_remote_references_and_zip_bombs(self):
        for href in ('../../../outside.xhtml','https://example.com/chapter','/absolute.xhtml','%2e%2e/%2e%2e/outside.xhtml'):
            with self.assertRaises(ValueError):
                safe_member('OEBPS',href)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'book.epub'
            make_epub(path,extra={'bomb.txt':'x'*100000})
            with self.assertRaisesRegex(ValueError,'budget'):
                epub_document(str(path))
            make_epub(path)
            with patch('org_roam_retrieval.formats.MAX_ARCHIVE_ENTRIES',2):
                with self.assertRaisesRegex(ValueError,'budget'):
                    epub_document(str(path))

    def test_pdf_aes_empty_open_password_and_real_password_are_distinct(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
        with tempfile.TemporaryDirectory() as directory:
            for password in ('','actual-password'):
                path=Path(directory)/('open.pdf' if not password else 'locked.pdf')
                writer=PdfWriter()
                page=writer.add_blank_page(width=600,height=800)
                font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
                page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
                stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 10 700 Td (galaxy AES text) Tj ET')
                page[NameObject('/Contents')]=writer._add_object(stream)
                writer.encrypt(user_password=password,owner_password='owner-only',algorithm='AES-256')
                writer.write(path)
                before=path.read_bytes()
                if password:
                    with self.assertRaisesRegex(ValueError,'Password-protected'):
                        document(str(path),{'enabled':False})
                else:
                    self.assertIn('galaxy AES text',document(str(path),{'enabled':False})['segments'][0]['text'])
                self.assertEqual(before,path.read_bytes())

    def test_exclusion_is_structural_not_small_image_or_math_topic(self):
        self.assertEqual('org-latex-preview',exclusion_reason('/notes/ltximg/org-ltximg_'+'a'*40+'.png'))
        self.assertEqual('org-latex-preview',exclusion_reason('/notes/previews/org-ltximg-'+'b'*40+'.svg'))
        for path in ('/notes/tiny.png','/notes/ltximg/authored-diagram.png','/notes/org-ltximg-notes.png','/notes/math-symbol.png'):
            self.assertIsNone(exclusion_reason(path),path)
        self.assertEqual('editor-backup-or-autosave',exclusion_reason('/notes/source.org~'))
        self.assertEqual('compiled-or-temporary',exclusion_reason('/notes/code.elc'))
        self.assertEqual('filename-only',document('/does/not/exist/unknown.binary')['coverage'])
        self.assertEqual('unsupported-format',document('/does/not/exist/unknown.binary')['details']['extraction_status'])


class IncrementalCoverage(unittest.TestCase):
    setUp = worker_tests.CorpusCase.setUp
    tearDown = worker_tests.CorpusCase.tearDown
    write = worker_tests.CorpusCase.write
    refresh = worker_tests.CorpusCase.refresh
    search = worker_tests.CorpusCase.search

    def test_archive_org_uses_parser_and_retains_original_latex(self):
        self.write('archived.org_archive','#+title: Archived Math\n* Original\n:PROPERTIES:\n:ID: archive-node\n:END:\nAstronomy formula \\(E=mc^2\\).\n')
        self.refresh()
        result=self.search('Astronomy',mode='lexical')['results'][0]
        self.assertEqual('archive-node',result['locator']['id'])
        self.assertIn('E=mc^2',result['excerpt'])
        self.assertEqual('text',result['coverage'])

    def test_old_schema_migration_reuses_vectors_and_excludes_generated_previews(self):
        text_path=self.write('unchanged.txt','galaxy astronomy source')
        tex_path=self.write('formula.tex','\\begin{document}\nStellar formula \\(E=mc^2\\).\n\\end{document}')
        preview_path=self.write('ltximg/org-ltximg_'+'a'*40+'.png','generated preview bytes')
        store=self.service.store
        for path,title,text,coverage in ((text_path,'unchanged','galaxy astronomy source','text'),
                                         (tex_path,'formula','formula','filename-only'),
                                         (preview_path,'bad OCR','spurious preview OCR text','ocr-text')):
            source={'path':path,'title':title,'coverage':coverage,'segments':[{'text':text,'title':title,'locator':{'kind':'file'}}]}
            store.upsert(source,'notes',self.service.stamp(path),hashlib.sha256(Path(path).read_bytes()).hexdigest())
        pending=store.missing_embeddings(100)
        store.embed(pending,self.service.encoder.documents([row['input'] for row in pending]))
        unchanged=store.db.execute('SELECT id,input_hash FROM chunks WHERE source_id=(SELECT id FROM sources WHERE path=?)',(text_path,)).fetchone()
        before_vector=store.db.execute('SELECT vector FROM embeddings WHERE input_hash=?',(unchanged['input_hash'],)).fetchone()[0]
        before_files={path:Path(path).read_bytes() for path in (text_path,tex_path,preview_path)}
        store.close()
        connection=sqlite3.connect(str(self.root/'index.sqlite'))
        connection.execute('ALTER TABLE sources DROP COLUMN extraction_policy')
        connection.execute('DROP TABLE excluded_sources')
        connection.execute('PRAGMA user_version=1')
        connection.commit();connection.close()
        self.service.store=Store(str(self.root/'index.sqlite'),Profile(dimension=16))
        self.service.encoder.calls=0
        self.refresh()
        after=self.service.store.db.execute('SELECT id,input_hash FROM chunks WHERE source_id=(SELECT id FROM sources WHERE path=?)',(text_path,)).fetchone()
        self.assertEqual(tuple(unchanged),tuple(after))
        vector=self.service.store.db.execute('SELECT vector FROM embeddings WHERE input_hash=?',(after['input_hash'],)).fetchone()[0]
        self.assertEqual(before_vector,vector)
        self.assertEqual(1,self.service.encoder.calls)  # Only newly extracted TeX prose needs vectors.
        self.assertFalse(self.search('spurious preview',mode='lexical')['results'])
        self.assertTrue(self.search('Stellar formula',mode='lexical')['results'])
        coverage=self.service.store.coverage()
        self.assertEqual(1,coverage['excluded_sources'])
        self.assertEqual(2,coverage['eligible_chunks'])
        self.assertEqual(2,coverage['embedded_chunks'])
        self.assertEqual(0,coverage['pending_embedding_chunks'])
        self.assertEqual(0,coverage['filename_only_chunks'])
        for path,content in before_files.items():
            self.assertEqual(content,Path(path).read_bytes())

    def test_denominator_excludes_filename_only_and_generated_artifacts(self):
        self.write('text.txt','galaxy stars')
        self.write('unknown.binary','binarynotprose')
        self.write('ltximg/org-ltximg_'+'b'*40+'.png','not rendered nor OCRd')
        self.refresh()
        coverage=self.service.store.coverage()
        self.assertEqual(2,coverage['chunks'])
        self.assertEqual(1,coverage['eligible_chunks'])
        self.assertEqual(1,coverage['embedded_chunks'])
        self.assertEqual(1,coverage['filename_only_sources'])
        self.assertEqual(1,coverage['unsupported_sources'])
        self.assertEqual({'org-latex-preview':1},coverage['excluded_by_reason'])
        generation=self.service.store.generation
        self.refresh()
        self.assertEqual(generation,self.service.store.generation)


if __name__=='__main__':
    unittest.main()
