#!/usr/bin/env python3
"""Small reproducible multilingual retrieval gate; no private corpus is read.

The fixtures are synthetic engineering checks, not user-judged quality evidence.
Run in the managed environment: python evaluate.py --device cpu --runs 5.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import resource
import statistics
import tempfile
import time

import numpy as np

from org_roam_retrieval.embedding import Profile, SentenceTransformerEncoder, validate_vectors
from org_roam_retrieval.store import Store

DOCUMENTS = [
    ('space', 'Galaxies and stars', 'Astronomy studies distant galaxies, stellar light, telescopes, and the expanding universe.'),
    ('bread', 'Making sourdough bread', 'A baker mixes flour and water, lets the dough ferment with yeast, and bakes a crusty loaf.'),
    ('river', 'River ecology', 'Flowing freshwater connects mountain streams to rivers and supports fish habitats.'),
    ('memory', 'Computer memory', 'Programs allocate RAM to store temporary data while a computer executes instructions.'),
    ('hebrew', 'חירות ושחרור', 'יציאה מעבדות לחירות מאפשרת לאדם להשתחרר משעבוד ולבחור את דרכו.'),
    ('greek', 'Ἀγάπη καὶ πλησίον', 'Ἀγαπᾶν τὸν πλησίον σημαίνει φροντίζειν ὑπὲρ τοῦ ἑτέρου καὶ βοηθεῖν αὐτῷ.'),
    ('meeting', 'Work meeting notes', 'The team discussed project deadlines, assigned tasks, and scheduled next week’s meeting.'),
    ('celebrity', 'Stars in entertainment', 'Famous movie stars attend award ceremonies, give interviews, and perform in popular films.'),
    ('finance', 'Bank accounts and lending', 'A bank keeps savings accounts, offers loans with interest, and transfers money between customers.'),
    ('neuro', 'Human memory and learning', 'The brain forms memories through neural connections, allowing people to remember past experiences and learned skills.'),
    ('garden', 'Garden planting', 'Plant tomato seeds in moist soil and give the young plants sunlight and regular watering.'),
]
QUERIES = [
    ('space-en', 'What can we learn by observing faraway celestial objects?', 'space'),
    ('bread-en', 'How do I ferment dough for a homemade loaf?', 'bread'),
    ('river-he', 'איפה חיים דגים במים זורמים?', 'river'),
    ('hebrew-he', 'שחרור מעבדות ובחירה חופשית', 'hebrew'),
    ('greek-en', 'Caring for your neighbor and helping another person', 'greek'),
    ('greek-gr', 'ἀγάπη τοῦ πλησίον', 'greek'),
    ('memory-en', 'Where does a running program keep temporary data?', 'memory'),
    ('bank-river', 'The banks of a stream shelter fish and other freshwater animals', 'river'),
    ('bank-finance', 'Borrowing money and paying interest on a loan', 'finance'),
    ('memory-human', 'How does a person remember events and learn new skills?', 'neuro'),
    ('related-space', 'We collected light through a telescope to study the evolution of the cosmos and faraway stellar systems.', 'space'),
    ('related-bread-he', 'אני מערבב קמח ומים וממתין לתסיסה לפני האפייה.', 'bread'),
]


def summarize(values):
    ordered = sorted(values)
    return {'median_ms': statistics.median(ordered), 'min_ms': ordered[0], 'max_ms': ordered[-1],
            'p95_ms': ordered[min(len(ordered)-1, math.ceil(.95*len(ordered))-1)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', choices=['cpu', 'mps'], default='cpu')
    parser.add_argument('--dimension', type=int, default=256)
    parser.add_argument('--runs', type=int, default=5)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    profile = Profile(device=args.device, dimension=args.dimension)
    encoder = SentenceTransformerEncoder(profile)
    started = time.perf_counter()
    vectors = encoder.documents([f'title: {title} | text: {text}' for _,title,text in DOCUMENTS])
    initial_ms = (time.perf_counter()-started)*1000
    validate_vectors(vectors, profile.dimension)
    rows = []
    query_times = []
    with tempfile.TemporaryDirectory() as directory:
        store = Store(directory+'/eval.sqlite', profile)
        identities = {}
        for key,title,text in DOCUMENTS:
            path = directory + '/' + key + '.txt'
            identities[path] = key
            store.upsert({'path':path,'title':title,'segments':[{'text':text,'title':title,'locator':{'kind':'text-range','line':1}}]},'fixtures','1',key)
        pending = store.missing_embeddings(100)
        by_input = {f'title: {title} | text: {text}': vector for (_,title,text), vector in zip(DOCUMENTS,vectors,strict=True)}
        store.embed(pending,[by_input[row['input']] for row in pending])
        for name,text,expected in QUERIES:
            lexical_ids = [identities[store.result(key)['path']] for key,_ in store.lexical_candidates(text)]
            started = time.perf_counter()
            query = encoder.query(text)
            elapsed = (time.perf_counter()-started)*1000
            query_times.append(elapsed)
            ranked = [identities[store.result(key)['path']] for key,_ in store.vector_candidates(query)]
            rank = ranked.index(expected)+1
            rows.append({'query':name,'expected':expected,'semantic_rank':rank,
                         'lexical_rank':lexical_ids.index(expected)+1 if expected in lexical_ids else None,
                         'top_result':ranked[0], 'query_ms':elapsed})
        warm = []
        for _ in range(args.runs):
            for _,text,_ in QUERIES:
                started = time.perf_counter()
                found = store.vector_candidates(encoder.query(text))
                assert len(found)==len(DOCUMENTS)
                warm.append((time.perf_counter()-started)*1000)
        store.close()
    report = {'fixture_kind':'synthetic engineering gate, not user-judged evaluation',
              'profile':asdict(profile), 'documents':len(DOCUMENTS),'queries':len(QUERIES),
              'model_load_plus_initial_documents_ms':initial_ms,
              'uncached_query':summarize(query_times), 'warm_cached_query_and_native_scoring':summarize(warm),
              'semantic_recall_at_3':sum(row['semantic_rank']<=3 for row in rows)/len(rows),
              'semantic_recall_at_1':sum(row['semantic_rank']==1 for row in rows)/len(rows),
              'lexical_recall_at_3':sum(row['lexical_rank'] is not None and row['lexical_rank']<=3 for row in rows)/len(rows),
              'semantic_ndcg_at_3':sum(1/math.log2(row['semantic_rank']+1) if row['semantic_rank']<=3 else 0 for row in rows)/len(rows),
              'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'checks': {'finite':bool(np.isfinite(vectors).all()),'dimensions':list(vectors.shape),
                         'max_norm_error':float(np.max(np.abs(np.linalg.norm(vectors,axis=1)-1)))},
              'results':rows}
    if args.output:
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if report['semantic_recall_at_3'] < .8:
        raise SystemExit('Synthetic multilingual quality gate failed')


if __name__=='__main__':
    main()
