# Reference only: the script used to extract seed_data.json from the two source files. Paths point at the original working folder.
"""Build backend seed JSON from the extracted workflow + question set."""
import json, re, html, sys
SP = '/tmp/claude-0/-home-claude/a442b2ce-0827-5f43-b961-fd2b05bd7d49/scratchpad'
OUT = sys.argv[1]

ex = json.load(open(f'{SP}/extract.json'))
ev = json.load(open(f'{SP}/evidence.json'))
js = open(f'{SP}/wf/script1.js').read()
QD = json.loads(re.search(r'var QD=(\[.*?\]\]);', js).group(1))
SPEC = json.loads(re.search(r'var SPEC=(\{.*?\});', js).group(1))
qd_src = {q[0]: [s for s in q[4].split(',') if s] for q in QD}

# ---------- dimension definitions (anchor / evidence / boundary) ----------
dd = open(f'{SP}/wf/dimension-definitions.html.txt').read().split('\n')
dd = [l.strip() for l in dd if l.strip()]

dims = [
    dict(code='D1', name='Discoverability & AEO Readiness', short='Discoverability & AEO', color='#0F6CBD',
         summary='Whether answer engines and shoppers can find, read and trust the retailer\'s product pages.'),
    dict(code='D2', name='Data & Content Foundation', short='Data & content', color='#107C10',
         summary='Whether product, price, stock and policy data is structured, complete and joined up.'),
    dict(code='D3', name='AI & Agent Adoption', short='AI & agent adoption', color='#8764B8',
         summary='Whether AI assistants and agentic workflows are live for customers or colleagues.'),
    dict(code='D4', name='Operating Model & Measurement', short='Operating model & measurement', color='#C4511F',
         summary='Whether AI has named owners, training, and measured outcomes. Merges the former D4 and D5.'),
]

def parse_def(code, nxt):
    i = dd.index(code); j = dd.index(nxt) if nxt else len(dd)
    block = dd[i:j]
    rows = []
    k = block.index('Boundary') + 1
    while k < len(block):
        if block[k].isdigit():
            lv = int(block[k]); parts = []
            k += 1
            while k < len(block) and not (block[k].isdigit() and len(block[k]) == 1):
                parts.append(block[k]); k += 1
            rows.append((lv, parts))
        else:
            k += 1
    return block, rows

defs = {}
for idx, d in enumerate(['D1', 'D2', 'D3', 'D4']):
    nxt = ['D2', 'D3', 'D4', None][idx]
    block, rows = parse_def(d, nxt)
    out = []
    for lv, p in rows:
        if d != 'D4':
            out.append(dict(level=lv, anchor=p[0], evidence=p[1], boundary=p[2]))
        else:
            # Operating model | text | Measurement | text  (x2) then boundary
            txt = ' '.join(p)
            m = re.findall(r'Operating model\|?\s*(.*?)\s*Measurement\|?\s*(.*?)(?=Operating model|$)', '|'.join(p).replace('|', ' '))
            out.append(dict(level=lv, raw=p))
    defs[d] = out

# D4 parse manually from raw lists
for r in defs['D4']:
    p = r.pop('raw')
    # p like ['Operating model', ' A local sponsor...', 'Measurement', ' A confirmed ...', 'Operating model', '...', 'Measurement', '...', 'boundary']
    vals = [x.strip() for x in p]
    om = [vals[i + 1] for i, v in enumerate(vals) if v == 'Operating model']
    ms = [vals[i + 1] for i, v in enumerate(vals) if v == 'Measurement']
    r['anchor'] = f'Operating model: {om[0]} Measurement: {ms[0]}'
    r['evidence'] = f'Operating model: {om[1]} Measurement: {ms[1]}'
    r['boundary'] = vals[-1]
    if 'Applies to' in r['boundary'] or r['boundary'] in ('',):
        pass

level_req = {
    'D1': ['Product pages exist and are findable in the market, with discoverability gaps noted',
           'Structured product content, search, filters and comparison support product selection',
           'AI or conversational discovery live in the market, confirmed by observation or a primary source',
           'Visibility and accuracy audited on a repeat cycle, with a named owner and a record of fixes',
           'Continuous improvement, accuracy sustained, and commercial uplift shown against a control'],
    'D2': ['Product records exist for sampled SKUs, with inconsistencies noted',
           'Required fields (ID, specs, price, terms) present across a defined product scope',
           'A live workflow combines two or more data types, e.g. product with stock, order or support',
           'Data quality and freshness managed to owned KPIs over two or more cycles, with an incident log',
           'Automated exception detection; data-related failures falling over three or more cycles'],
    'D3': ['At least one documented AI experiment by the retailer (selling AI devices does not count)',
           'A named AI initiative with a defined scope, partner or plan date',
           'At least one AI workflow live in a stated customer or colleague setting',
           'Two or more live workflows with usage, monitoring and incident data over two or more cycles',
           'Three or more governed workflows with change control and sustained outcomes'],
    'D4': ['Named local sponsor or expert for ≥1 AI activity; ≥1 AI use case with an ad hoc reported result',
           'Named AI owner, delivery partner or defined training programme; ≥1 AI metric with defined numerator and period',
           'Decision rights and delivery cadence documented; role-based training running; ≥1 use-case outcome with numerator, denominator and scope',
           'Proficiency reviewed against priorities over ≥2 cycles; baseline + comparison group reviewed over ≥2 cycles',
           'Documented resourcing or standards changes from reviews; replicated effect incl. cost & return; a changed investment decision'],
}
cells = []
for d in dims:
    for lv in range(1, 6):
        s = SPEC[f"{d['code']}-L{lv}"]
        df = next(x for x in defs[d['code']] if x['level'] == lv)
        cells.append(dict(dimension=d['code'], level=lv, name=s[0], stage=s[1], test=s[2], threshold=s[3],
                          requirement=level_req[d['code']][lv - 1], anchor=df['anchor'], evidence=df['evidence'], boundary=df['boundary']))

levels = [
    dict(level=1, name='Ad hoc', stage='Crawl', meaning='A confirmed basic or isolated practice with limited repeatability', min_evidence='Positive evidence of the practice and its limited scope. Silence never qualifies.'),
    dict(level=2, name='Defined', stage='Crawl', meaning='An identifiable capability, initiative or owner', min_evidence='A concrete scope and a traceable description or observable example.'),
    dict(level=3, name='Operational', stage='Walk', meaning='A live workflow or repeatable operating capability', min_evidence='Evidence of use in a stated scope. A future rollout announcement is insufficient.'),
    dict(level=4, name='Managed', stage='Run', meaning='A scaled capability with controls and measured performance', min_evidence='Operating and outcome evidence across more than one cycle, plus named accountability.'),
    dict(level=5, name='Adaptive', stage='Run', meaning='Continuous improvement and sustained value across the agreed scope', min_evidence='Repeated learning, governed changes and durable outcomes. A vendor claim of leadership is insufficient.'),
]

# ---------- questions ----------
lens_map = {'MATURITY': 'Maturity', 'COMPETITIVE – vendor': 'Competitive · vendor', 'COMPETITIVE – retailer': 'Competitive · retailer'}
questions = []
for q in ex['questions']:
    bb = ex['bestbuy'].get(q['qid'])
    questions.append(dict(
        qid=q['qid'], dimension=q['dim'], sub_theme=q['sub_theme'], text=q['question'],
        lens=(bb['lens_wf'] if bb else lens_map[q['lens']]),
        level=(bb['test_level'] if bb and bb['test_level'] else (int(q['level']) if str(q['level']).isdigit() else None)),
        change=q['change'], prev_id=q['prev_id'], evidence_type=q['evidence_type'], how=q['how'],
        answer_type=q['answer_type'], cadence=q['cadence'],
        in_run=bool(bb), origin=(bb['origin'] if bb else None)))

bestbuy = []
for qid, r in ex['bestbuy'].items():
    bestbuy.append(dict(qid=qid, status=r['status'], finding=r['finding'], sources=qd_src.get(qid, r['sources']), flags=r['flags']))

# ---------- evidence register ----------
v2 = ev['V'][1]['rec']
recs = []
stmap = {'acc': 'Accepted', 'adj': 'Adjudication', 'ref': 'Needs refresh', 'unk': 'Unknown', 'rev': 'Revalidate', 'rej': 'Rejected for cell', 'raw': 'Fetched', 'hum': 'Human-verified'}
for r in ev['RECS']:
    v = v2[r['id']]
    recs.append(dict(eid=r['id'], title=r['title'], claim=r['claim'], publisher=r['pub'], url=r['url'], pub_date=r['pubDate'], observed=r['obs'],
                     channel=r['channel'], category=r['category'], use_case=r['useCase'], stage=r['stage'], independence=r['indep'], cell=r['cell'],
                     rationale=r['rationale'], limits=r['limits'], source_type=r['src'], confidence=r['conf'], result=r['result'], h_flag=bool(r.get('h')),
                     register_status=stmap[v[0]], register_note=v[1], freshness=v[3], change=v[4] if len(v) > 4 else '',
                     val_owner=r['val']['owner'], val_proof=r['val']['proof'], val_due=r['val']['due'], val_reviewer=r['val']['reviewer'], val_disposition=r['val']['disp'],
                     next_level=r['next'], questions=[], sources=[]))
for k, b in ev['BASE'].items():
    found = next((x for x in recs if x['eid'] == k), None)
    if found:
        found['questions'] = b[2].split(); found['sources'] = b[3].split()
    else:
        v = v2[k]
        extra = {
            'EV-011': dict(stage='Live', channel='Website / app', use_case='Customer support', pub_date='2024', next_level='Revalidate: 2024 evidence is older than 12 months.'),
            'EV-012': dict(stage='Plan', channel='Stores', use_case='Associate assist', pub_date='2024', next_level='A plan does not establish Level 3.'),
            'EV-013': dict(stage='Plan', channel='Website / app', use_case='AI search', pub_date='2025-05', next_level='Revalidate: May 2025 announcement is older than 12 months.'),
        }[k]
        recs.append(dict(eid=k, title=b[0], claim=b[0], publisher='Best Buy', url='Public statement', pub_date=extra['pub_date'], observed='2026-10-09',
                         channel=extra['channel'], category='—', use_case=extra['use_case'], stage=extra['stage'], independence='Added by Validation node', cell=b[1],
                         rationale=v[4], limits=['Older than 12 months (H).'], source_type='Public statement', confidence=v[2], result='Pass' if v[0] != 'rej' else 'Unknown', h_flag=True,
                         register_status=stmap[v[0]], register_note=v[1], freshness=v[3], change=v[4],
                         val_owner='Assessor', val_proof='Find current (≤12 months) evidence or confirm H', val_due='2026-10-30', val_reviewer='QA reviewer', val_disposition='Open',
                         next_level=extra['next_level'], questions=b[2].split(), sources=b[3].split()))

validation_flags = [
    dict(area='Questions', kind='Coverage', severity='info', text='No maturity question tests Levels 4–5 in D1–D3 or Level 5 in D4, so D1–D3 can reach Level 3 at most and D4 Level 4. D3 has no Level 1 question and one at Level 2 (D3-03).', qids=['D3-03']),
    dict(area='Questions', kind='Merged answers', severity='warn', text='5 merged questions combine old answers that disagreed; the carried status needs a reviewer to confirm.', qids=['D1-26', 'D3-35', 'D4-07', 'D4-16', 'D4-23']),
    dict(area='Freshness', kind='H flag', severity='info', text='The Feb 2024 care-agent result carries the H flag.', qids=['D4-15', 'D4-16']),
    dict(area='Freshness', kind='Older than 12 months', severity='warn', text='Evidence older than 12 months without an H mark: May 2025 AI search, the 2024 support assistant and the 2024 associate-assistant plan.', qids=['D1-24', 'D3-02', 'D3-22', 'D3-30', 'D3-35']),
    dict(area='Freshness', kind='30-day refresh', severity='warn', text='Dynamic price and availability observations must be refreshed within 30 days of client publication. The delivery date captured was Tue, Sep 22.', qids=['D1-11', 'D1-14', 'D2-08', 'D2-18']),
    dict(area='Evidence', kind='Conflict', severity='warn', text='The snapshot-vs-live conflict keeps both claims as an Issue instead of picking the favorable one.', qids=['D1-11', 'D1-14']),
    dict(area='Evidence', kind='Missing source', severity='error', text='Answered without a source ID.', qids=['D1-06', 'D1-25', 'D1-34']),
    dict(area='Evidence', kind='Status conflict', severity='error', text='Status conflicts with the finding, which says the check can still be run, so the status should be Check pending. If changed, Check pending rises from 65 to 69 and the status counts need updating.', qids=['D1-06', 'D1-12', 'D1-25', 'D1-45']),
    dict(area='Evidence', kind='Unsupported Yes', severity='error', text='Marked Yes, but the finding says the assistant was not tested. Yes needs cited evidence.', qids=['D1-34']),
    dict(area='Dimensions', kind='Sample size', severity='warn', text='D1-L1 needs ≥5 sampled product pages, but only 1 page was fully fetched.', qids=['D1-01']),
    dict(area='Dimensions', kind='Sample size', severity='warn', text='D2-L2 needs ≥80% of sampled SKUs with required fields, but the sample is 1 SKU.', qids=['D2-03']),
]

guide = dict(
    status_definitions=[
        dict(status='Yes', meaning='The test is met, with cited evidence', open='No'),
        dict(status='Partial', meaning='Partly met, or met on a limited sample', open='Optional. A wider sample would firm it up.'),
        dict(status='Issue', meaning='We checked and found a defect, not an absence', open='No. This is a finding to report.'),
        dict(status='No', meaning='We checked and the answer is no: the information is public and does not meet the test', open='No'),
        dict(status='Not disclosed', meaning='The retailer does not publish this either way. A statement about the public record, not about the retailer\'s capability.', open='No. Reported as a limit.'),
        dict(status='Check pending', meaning='A public-source check not yet run: new questions plus carried checks (robots.txt, sitemap, wider page sample)', open='Yes. The open work for the first run on the v4 set.'),
        dict(status='N/A', meaning='The question does not apply to this assessment (e.g. other markets, out of scope)', open='No'),
    ],
    evidence_fields=[
        dict(field='Evidence identity', content='Stable ID, source URL or file, publisher, title, publication date and observation date.'),
        dict(field='Claim and scope', content='Short factual observation, retailer, country, channel, category and use case.'),
        dict(field='Stage', content='Plan, pilot, live, scaled or measured. Do not infer a later stage.'),
        dict(field='Independence', content='Original event ID and whether another source merely repeats the same announcement.'),
        dict(field='Assessment link', content='Dimension, candidate level, accepted or rejected status and rationale.'),
        dict(field='Limitations', content='Missing denominator, unclear deployment scope, older source, contradiction or inaccessible evidence.'),
        dict(field='Validation', content='Owner, requested proof, due date, reviewer and final disposition.'),
    ],
    freshness=[
        dict(evidence='Dynamic product, price, availability and customer-journey evidence', window='30 days', action='Refresh within 30 days of client publication.'),
        dict(evidence='Deployment, operating-model and measurement evidence', window='12 months', action='Revalidate evidence older than 12 months.'),
        dict(evidence='Older evidence', window='H', action='Retain for history but mark it H.'),
        dict(evidence='Repeated announcements', window='—', action='A new article that repeats an old announcement does not reset the underlying evidence date.'),
    ],
    confidence=[
        dict(level='High', meaning='Current evidence with clear scope and direct observation or independent operational corroboration.'),
        dict(level='Medium', meaning='An explicit primary-source statement with an important validation gap.'),
        dict(level='Low', meaning='Historical or indirect evidence.'),
        dict(level='U', meaning='Missing evidence.'),
    ],
    conflicts=[
        dict(rule='Keep both claims', action='Preserve both claims and identify the time or scope difference.'),
        dict(rule='Anchor affected', action='If the conflict changes whether an anchor is met, leave the affected level unresolved until adjudication.'),
        dict(rule='No cherry-picking', action='Do not select the more favorable claim.'),
        dict(rule='Hiring posts', action='Do not treat hiring posts as evidence of deployed capability; they can help identify a skill or investment hypothesis.'),
    ],
    scoring=dict(weights={'Yes': 1, 'Partial': 0.5, 'Issue': 0.5, 'No': 0, 'Not disclosed': 0}, threshold=35, infer=True,
                 rule='Each level has a test. Work up from Level 1 and stop at the first level not met — that is the score. A strong signal at a higher level cannot make up for a missing one below it. If Level 1 is not met, the dimension is recorded as Not established rather than zero.',
                 labels='The integer is the highest level met in sequence; the decimals show progress toward the next level (its weighted evidence ÷ the level threshold, capped at .99; .00 when the next level has no questions). H marks key evidence older than 12 months. Example: L1.40 H = Level 1 met, 40% of the way to Level 2, on evidence older than 12 months.',
                 excluded='Check pending, N/A and competitive (vendor or retailer) questions are left out of the denominator.',
                 inference='A live Level 3 workflow implies the Level 1 and 2 anchors, so by default an untested lower level is inferred from the tested level above it. Untested top levels are never inferred: they are the ceiling.'),
    routes=[
        dict(id=1, name='Customer-facing retailer or direct channel', scope='Local product discovery, purchase and service journeys', emphasis='Answer accuracy, catalog quality, live AI usage, ownership and customer outcomes.'),
        dict(id=2, name='Distributor or reseller platform', scope='Partner discovery, product information, ordering and enablement', emphasis='Partner-facing content, operational integrations, AI workflow adoption and service outcomes.'),
        dict(id=3, name='Subscription, payment or other strategic partner', scope='Partner onboarding, offer delivery, activation or service', emphasis='Offer accuracy, integration quality, live workflow use and measurable partner outcomes.'),
    ],
    journey=[
        dict(stage='Discover', question='Can the customer find a relevant eligible product? Record the surfaced answer, source, locale and product coverage.', dims=['D1', 'D2'], connection='D1 discovery and D2 content'),
        dict(stage='Compare', question='Can the customer distinguish capabilities, compatibility and subscription requirements? Check claims against dated authoritative product terms.', dims=['D2', 'D3'], connection='D2 content and D3 assistance'),
        dict(stage='Purchase', question='Does the documented journey preserve product identity and terms through the available checkout steps? Do not complete a purchase for an assessment.', dims=['D2', 'D3'], connection='D2 integration and D3 workflow'),
        dict(stage='Activate', question='Are setup, entitlement and support handoffs clear within the partner\'s remit? Observe only with authorized test access.', dims=['D2', 'D4'], connection='D2 content and D4 operating model'),
        dict(stage='Use and support', question='Can customers resolve the defined issue, and does the owner measure the result? Require aggregate workflow evidence for outcome claims.', dims=['D3', 'D4'], connection='D3 operation, D4 operating model and measurement'),
    ],
    distributor_journey=['Partner discovery', 'Solution configuration', 'Quotation and order', 'Provisioning', 'Renewal or support'],
    journey_note='A journey observation informs relevant rubric anchors; it does not create another averaged maturity score.',
    distributor_note='Replace the consumer journey with these stages. Do not score a distributor against consumer checkout obligations it does not own.',
    run_parameters=[
        dict(key='[VENDOR]', value='Microsoft', notes='The client vendor whose products are being assessed at the retailer. Change this to run the set for another vendor.'),
        dict(key='[VENDOR SKUS]', value='Surface devices, Copilot+ PCs, Windows 11 PCs, Microsoft 365', notes='The product families searched for and sampled.'),
        dict(key='[COMPETING VENDORS]', value='Apple, Dell, HP, Lenovo, Samsung', notes="Brands the vendor is compared against inside the retailer's own estate."),
        dict(key='[PEER RETAILERS]', value='Set per market', notes='Retailers this retailer is benchmarked against. Set per market.'),
        dict(key='Sample', value='30 SKUs: 10 [VENDOR], 10 Apple, 10 other — matched by price band and category', notes='Fixed and reused each cycle so results are comparable.'),
        dict(key='Prompt panel', value="15 buying prompts, 10 'where to buy', 8 policy questions", notes='Fixed wording, reused each cycle across all AI engines.'),
        dict(key='Market', value='Single market per run', notes='No rating transfers across markets.'),
    ],
    readme=[dict(k=k, v=v) for k, v in [
        ('What this is', "The final question set, built from three independent reworkings of the original 229: the C5i v2 shortlist, Rajasekar's set and Mayank's set. Where all three agreed, the decision stood. Where they differed, the deciding rule was whether an agent can retrieve the answer from a public source on a monthly cycle."),
        ('Two competitive axes', "OEM axis: how [VENDOR] products are treated against [COMPETING VENDORS] inside this retailer. Retailer axis: how this retailer performs against [PEER RETAILERS]."),
        ('Competitive questions are not scored', 'They are reported alongside the maturity score, never added to it, so scores stay comparable across retailers and markets.'),
        ('The agentic rule', 'Every question carries a retrieval method, an evidence type, an answer type and a cadence. Anything that would require an interview, an internal document or a judgement call has been removed.'),
    ]],
    removed=ex['removed'], not_adopted=ex['not_adopted'],
    stop_reasons={
        'D1': 'Level 3 is the ceiling of the 158-question set for D1: no public-source maturity question sits at Level 4 or 5. Under the 229-question set, Level 4 needed repeat visibility and accuracy audits with a named owner, which Best Buy does not publish.',
        'D2': 'Level 3 is the ceiling for D2. Level 4 (owned data-quality KPIs and an incident log) is not tested by any public-source question.',
        'D3': 'Level 3 is the ceiling for D3. Level 1 has no question and is inferred from the live Level 3 workflows (agentic checkout, support assistant, Ask Blue).',
        'D4': 'Level 4 needs AI capacity managed against priorities and outcomes tested against a comparison. The two Level 4 questions score 25%: AI-specific funding is not disclosed (D4-10) and no comparison-group result is published (D4-20).',
    },
)

markets = [
    dict(name='United States', code='US', currency='USD', market_maker=7, strategic=13),
    dict(name='Canada', code='CA', currency='CAD', market_maker=6, strategic=4),
    dict(name='Japan', code='JP', currency='JPY', market_maker=4, strategic=7),
    dict(name='Australia', code='AU', currency='AUD', market_maker=7, strategic=8),
    dict(name='Germany', code='DE', currency='EUR', market_maker=3, strategic=4),
    dict(name='United Kingdom', code='GB', currency='GBP', market_maker=3, strategic=8),
    dict(name='France', code='FR', currency='EUR', market_maker=3, strategic=4),
    dict(name='Italy', code='IT', currency='EUR', market_maker=4, strategic=3),
]
peers = {
    'US': ['Walmart US', 'Costco US', 'Micro Center US', 'Amazon US', 'NewEgg US', 'Target US'],
    'CA': ['Staples Canada', 'Costco Canada', 'Amazon Canada', 'Canada Computers', 'Walmart Canada'],
    'JP': ['Bic Camera', 'Yamada Denki', 'Edion', 'Amazon Japan', 'K\'s Denki'],
    'AU': ['Harvey Norman', 'The Good Guys', 'Officeworks', 'Amazon Australia', 'Bing Lee'],
    'GB': ['Argos', 'John Lewis', 'AO.com', 'Amazon UK', 'Very'],
}
retailers = [
    dict(slug='best-buy', name='Best Buy', hq='US', route=1, segment='Market Maker', markets=['US', 'CA'], profile=0.78),
    dict(slug='currys', name='Currys', hq='GB', route=1, segment='Market Maker', markets=['GB'], profile=0.62),
    dict(slug='yodobashi', name='Yodobashi Camera', hq='JP', route=1, segment='Strategic', markets=['JP'], profile=0.5),
    dict(slug='jb-hi-fi', name='JB Hi-Fi', hq='AU', route=1, segment='Market Maker', markets=['AU'], profile=0.56),
    dict(slug='ingram-micro', name='Ingram Micro', hq='CA', route=2, segment='Strategic', markets=['CA', 'US'], profile=0.6),
]
months = ['2026-07', '2026-08', '2026-09', '2026-10']

json.dump(dict(dimensions=dims, levels=levels, cells=cells, questions=questions, bestbuy_us_2026_10=bestbuy,
               evidence_bestbuy=recs, validation_flags=validation_flags, guide=guide, markets=markets, peers=peers,
               retailers=retailers, months=months, baseline_229={'D1': 'L3', 'D2': 'L3', 'D3': 'L3', 'D4': 'L1 H'}),
          open(OUT, 'w'), ensure_ascii=False, indent=1)
print('ok', len(questions), len(bestbuy), len(recs), len(cells))
print(defs['D4'][0])
