"""Compile observed read-only WAT findings; never contacts providers."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'outputs/wat-pilot-20261010/real-output-shadow'
tasks = {i:json.loads((OUT/f'task-{i}/native-task-and-comments.json').read_text()) for i in (10309337809,10064527849)}
old = json.loads((OUT/'existing-file-revalidation.json').read_text())
new = json.loads((OUT/'selected-file-inspection.json').read_text())
for row in old:
    row['visually_reviewed_current_pass'] = True
    row['native_bytes_redownloaded_this_audit'] = False
for row in new:
    row['visually_reviewed_current_pass'] = True
    row['native_bytes_redownloaded_this_audit'] = True
all_files = old + new
for row in all_files:
    data = Path(row['path']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == row['sha256']
    assert row['image_decode_verified'] is True
for name,rows in [('existing-file-revalidation.json',old),('selected-file-inspection.json',new)]:
    (OUT/name).write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')

summary = []
for task_id,t in tasks.items():
    comments=t['comments']; files=[a for c in comments for a in c['attachments']]
    assert len(comments)==t['comments_count']
    summary.append({'task_id':task_id,'title':t['title'],'task_completed':t['completed'],
      'comments_complete':True,'comments_count':len(comments),'native_comment_attachments':len(files),
      'native_description_attachments':len(t['description_attachments']),
      'attachment_types':dict(Counter(a['content_type'] for a in files)),
      'comment_producer_ids':sorted({c['creator']['id'] for c in comments if c['attachments']}),
      'samet_comments':sum(c['creator']['id']==50486696 for c in comments),
      'native_video_attachments':sum(a['content_type'].startswith('video/') for a in files),
      'native_pdf_attachments':sum(a['content_type']=='application/pdf' for a in files),
      'all_attachment_bytes_downloaded':False,'customer_email_delivery_verified':False,
      'source_read_at':t['read_evidence']['recorded_at']})

def artifact(selection):
    row=next(x for x in all_files if x['selection']==selection)
    return {'selection':selection,'task_id':row['task_id'],'comment_id':int(row['comment_id']),
      'attachment_id':row.get('attachment_id') or row['attachment']['id'],
      'sha256':row['sha256'],'path':row['path'],'bytes':row.get('bytes',row.get('byte_size'))}

report={'schema_version':1,'recorded_at':datetime.now(timezone.utc).isoformat(),
  'source_type':'user_selected_historical_references_only',
  'scope':{'account_id':5898865,'project_id':40694338,'tasks':summary,'selected_pngs_visually_inspected':17,
    'new_native_downloads':11,'new_downloaded_bytes':sum(x['byte_size'] for x in new),
    'previous_local_files_hash_decode_checked':6,'business_writes':0,'mail_sends':0,'paid_model_calls':0,
    'signed_urls_or_tokens_persisted':False,'all_source_design_files_read':False},
  'production_identity':{
    'Berkay':{'id':47658175,'evidence':'Native creator of selected design upload comments in both tasks. Upload authorship is verified; no inference that every graphic was solely authored by him.'},
    'Samet':{'id':50486696,'verified_in_these_tasks':False,'evidence':'No native comments by Samet and no video MIME attachments in either complete comment list.'},
    'Ipek':{'id':51207148,'evidence':'Native creator of the final selected September desktop slider and extra 1000x666 deliverable; historical handoff is recorded.'}},
  'findings':[
    {'id':'october_missing_required_price','artifacts':[artifact('pre-price-kv'),artifact('approved-kv-candidate')],
      'brief_comment_ids':[10327675636,10317818651],
      'observed':'Pre-price PNG visibly lacks required 10,99 TL/kWh. Later candidate visibly contains it; campaign headline, one-week badge and final 7 Ekim disclaimer are present.',
      'safe_label':'Known factual omission for the first artifact; presence check only for later artifact, not total-quality pass.',
      'numeric_score':None},
    {'id':'october_approval_and_same_name_version','artifacts':[artifact('approved-kv-candidate'),artifact('final-package-kv')],
      'approval_comment_id':10328301172,'approver_id':52096503,
      'observed':'Özge says tasarım onaylandı after the candidate. Later package uses the same filename but differs in both bytes and pixels.',
      'binding':'Candidate association is chronological, not exact hash approval. Later package and cover have no separate explicit approval in the complete task comments.',
      'automatic_release_permitted':False},
    {'id':'october_artifact_purpose','artifacts':[artifact('final-package-story'),artifact('final-package-horizontal'),artifact('latest-reels-cover-candidate')],
      'observed':'Story is an actual 1080x1920 campaign layout. Horizontal is a 1920x1080 background scene without campaign copy. Latest cover is a 1080x1920 crop with disclaimer but not full campaign copy.',
      'safe_label':'Background/cover purpose needs its own brief; do not mark every attachment as a finished full campaign ad or send all attachments.',
      'remaining_unknown':'One telephone revision at comment10328077641 was not written down; original complete final brief cannot be reconstructed.'},
    {'id':'summer_creative_selection_overrides_chronological_guess',
      'artifacts':[artifact('berkay-before-summer-revision'),artifact('berkay-after-summer-revision'),artifact('ozge-selected-kv-reference')],
      'revision_comment_id':10066343162,'selection_comment_id':10069011369,'selector_id':52096503,
      'observed':'A summer-theme revision adds umbrella, beach ball, swim ring and foreground leaves. The next-day explicitly selected image attached by Özge does not contain these added items and visually follows the earlier cleaner approach.',
      'safe_label':'The selected attachment is directly bound to Özge selection, but is a separate 2048x1214 reference, not byte-identical approval of the 3240x1920 production file.',
      'consequence':'Do not turn the earlier creative criticism into a permanent universal failed-example label or assume latest uploaded design is selected.',
      'numeric_score':None},
    {'id':'storyboard_is_not_video','artifacts':[artifact('berkay-storyboard-latest')],
      'producer_id':47658175,'observed':'Actual file is one 1988x1112 PNG with three vertical storyboard panels. It has campaign frames and final social/store logo panel; no motion, duration, audio or video stream exists in this file.',
      'related_source_handoff_comment_ids':[10070247757,10074218937],
      'safe_label':'Verified storyboard/source handoff evidence only. Samet video production and render quality remain unverified.'},
    {'id':'summer_last_berkay_carousel',
      'artifacts':[artifact(f'berkay-last-carousel-{i}') for i in range(1,6)],
      'brief_comment_ids':[10254869933,10254611661],
      'observed':'Berkay uploaded five actual 1080x1350 PNGs: campaign cover; Ankara; Antalya+Bursa; Istanbul; Istanbul+Izmir+website CTA. The cover includes Eylül Boyunca, 10,99 and selected-DC disclaimer. Oksijen Tesisleri appears in the listed content.',
      'safe_label':'Visible structural response to the recorded five-city simplification. Exact location spelling/completeness and sorting were not verified against the undownloaded source XLSX.',
      'customer_final_approval_found':False,
      'brand_exception_note':'Red badge follows a documented historical request after a brand-color discussion; not a blanket current WAT palette exception.'},
    {'id':'latest_output_is_ipek_and_extra_size_not_defect',
      'artifacts':[artifact('ipek-last-desktop-slider'),artifact('ipek-last-added-cover')],
      'producer_id':51207148,'clarification_comment_ids':[10264266104,10273027864,10273085260],
      'observed':'Actual desktop slider is1920x570. The later artifact is1000x666. The request explicitly says original dimensions were not wrong and asks for an additional size.',
      'safe_label':'Existing correct-size artifact plus added deliverable, not a required-fix label. The task is completed but no later explicit approval of all files or email delivery proof is present.'}
  ],
  'brand_rule_application':{
    'current_guide_upload_id':10392828700,'current_guide_sha256':'6be9fc432313ed5fd6d7741d52e83e584dddd26b8802238a01aee8b05ca3881c',
    'confirmed_main_font':'Nunito Sans','confirmed_support_font':'Josefin Sans','confirmed_zircon_hex':'#E8E7E6',
    'comparison':'Selected visuals were reviewed alongside the now-confirmed guide. No exact font identity, logo geometry, palette/ICC tolerance, usage-rights verification or numerical brand quality score was established from raster appearance alone.',
    'historical_exceptions_are_current_blanket_rules':False},
  'open_inputs_for_root':[
    'A real WAT task with Samet-authored final render and its recorded brief/approval is needed to verify actual video production; these two references do not provide it.',
    'A small human-labeled acceptance set is still required to calibrate 80/90 routing; these observations alone do not calibrate numerical quality.',
    'Historical tasks do not establish current Gmail To/CC authority, late-delivery permission or real recipient delivery.'],
  'prohibitions_on_inference':['No live WAT customer acceptance granted.','No historical prices, dates, staff handoff or exceptions copied into current rules.','Completed task is not proof of approved full package or delivered email.']}
(OUT/'real-output-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'comments_verified':sum(x['comments_count'] for x in summary),'comment_attachments':sum(x['native_comment_attachments'] for x in summary),'description_attachments':sum(x['native_description_attachments'] for x in summary),'selected_pngs':len(all_files),'samet_or_video_found':False,'numeric_calibration_claimed':False,'evidence_report':str(OUT/'real-output-audit.json')},ensure_ascii=False))
