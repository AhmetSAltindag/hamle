import base64
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import hashlib,json,socket,unittest
from unittest.mock import patch
import v11_internal_providers as app
from test_cloud_providers import Session,Response,FAKE_TOKEN,Store,Gmail,Cred,message
from test_business_writers import task_receipt
TID=123456789
PNG=b'\x89PNG\r\n\x1a\nsynthetic-provider-test-bytes'
SGID='SyntheticInternalAttachment123456789'
def attachment():
    return {'provider_id':'333','identifier_kind':'native_id','attachable_sgid':SGID,'filename':'stage-1.png','content_type':'image/png','byte_size':len(PNG),'sha256':hashlib.sha256(PNG).hexdigest()}
def current():
    row=task_receipt();row['id']=TID;return row
def comment(body='Test',attached=False):
    return {'id':444,'type':'Comment','status':'active','visible_to_clients':False,'bucket':{'id':app.PROJECT},'parent':{'id':TID,'type':'Todo'},'creator':{'id':app.HAMLE_ID},
        'content':app.writers._description(body)+(f'<bc-attachment sgid="{SGID}"><figure>expanded file</figure></bc-attachment>' if attached else ''),
        'content_attachments':[{'id':333,'sgid':SGID,'filename':'stage-1.png','content_type':'image/png','byte_size':len(PNG),'download_url':'https://storage.3.basecamp.com/5898865/blobs/hash/download/stage-1.png'}] if attached else []}
def evidence():
    return {'subject':app.SUBJECT,'sender':app.provider.AHMET,'hamle_direct_recipient':True,'action_eligible':False,'thread_id':'abcdef123','message_id':'123abc','latest_to':[app.provider.HAMLE],'latest_cc':['cansu@gamechangers.com.tr'],'in_reply_to':'<root@example.invalid>','references':'<root@example.invalid>'}
def files():
    return [{'filename':'stage-1.png','data':PNG,'content_type':'image/png'},{'filename':'stage-2.png','data':PNG+b'2','content_type':'image/png'}]
class InternalProviderTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket','create_connection'):
            p=patch.object(socket,name,side_effect=AssertionError('No network'));p.start();self.addCleanup(p.stop)
        self.adapter=app.InternalProviders(object(),allowed_task_ids=[TID])
    @contextmanager
    def gmail(self):yield FAKE_TOKEN
    @contextmanager
    def session(self,session):
        with patch.object(app.provider,'_session',return_value=session),patch.object(self.adapter,'_basecamp',return_value=FAKE_TOKEN),patch.object(self.adapter.writer,'_basecamp',return_value=FAKE_TOKEN),patch.object(self.adapter.writer,'_gmail',self.gmail):yield session
    def error(self,code,callback):
        with self.assertRaises(app.InternalProviderError) as got:callback()
        self.assertEqual(got.exception.code,code);self.assertNotIn(FAKE_TOKEN,str(got.exception));self.assertFalse(got.exception.retry_allowed)
    def test_callback_required_for_all_mutations(self):
        for call in (lambda:self.adapter.create_task('x','y'),lambda:self.adapter.create_comment(TID,'x'),lambda:self.adapter.upload_attachment('x.png',PNG,'image/png'),lambda:self.adapter.send_delivery(evidence(),'x',files())):
            self.error('durable_before_post_required',call)
    def test_old_and_unbound_tasks_block_before_network(self):
        for tid in app.OLD_TASKS:
            self.error('historical_task_forbidden',lambda:app.InternalProviders(object(),allowed_task_ids=[tid]));self.error('historical_task_forbidden',lambda:self.adapter.create_comment(tid,'x',before_post=lambda:None))
        self.error('task_not_durably_bound',lambda:self.adapter.create_comment(987,'x',before_post=lambda:None))
    def test_new_subject_capture_is_exact_and_default_unchanged(self):
        for subject in (app.SUBJECT,app.provider.INTERNAL_SUBJECT):
            gmail=Gmail([message(subject=subject)])
            r=app.provider.capture_internal_request(Store(),allow_body_for_internal_request=True,exact_subject=subject,_service_factory=lambda _:gmail,_credentials_factory=lambda _:Cred(),_request_factory=lambda:object(),clock=lambda:100)
            self.assertEqual(r['evidence']['subject'],subject);self.assertIn('subject:"'+subject+'"',next(x[1]['q'] for x in gmail.calls if x[0]=='list'))
    def test_new_subject_rejects_other_and_continuation(self):
        for subject,continuation,code in [('ANYTHING',None,'internal_subject_not_allowed'),(app.SUBJECT,{},'continuation_binding_invalid')]:
            with self.assertRaises(app.provider.ProviderError) as got:app.provider.capture_internal_request(object(),allow_body_for_internal_request=True,exact_subject=subject,continuation=continuation)
            self.assertEqual(got.exception.code,code)
    def test_capture_adapter_uses_argument_without_global_change(self):
        with patch.object(app.provider,'capture_internal_request',return_value={'capture':1}) as capture:
            self.assertEqual(self.adapter.capture_source(),{'capture':1});self.assertEqual(capture.call_args.kwargs['exact_subject'],app.SUBJECT);self.assertEqual(app.provider.INTERNAL_SUBJECT,'HAMLE-IC-IS-20261010-01')
    def test_task_reuses_existing_narrow_writer(self):
        with self.session(Session(Response(current(),status=201))) as s:r=self.adapter.create_task('Title','Body',before_post=lambda:None)
        self.assertEqual(r,{'provider_id':str(TID)});self.assertEqual(s.calls[0][1],app.PROJECT_BASE+f'/todosets/{app.PARENT}/todos.json');self.assertFalse(json.loads(s.calls[0][2]['data'])['notify'])
    def test_upload_raw_bytes_safe_receipt_and_final_fence(self):
        row={'id':333,'attachable_sgid':SGID,'filename':'stage-1.png','content_type':'image/png','byte_size':len(PNG),'download_url':'https://never-output.invalid/signed'};events=[]
        with self.session(Session(Response(row,status=201))) as s:r=self.adapter.upload_attachment('stage-1.png',PNG,'image/png',before_post=lambda:events.append('fenced'))
        self.assertEqual(r,attachment());self.assertEqual(events,['fenced']);self.assertEqual(s.calls[0][2]['data'],PNG);self.assertEqual(s.calls[0][2]['headers']['Content-Length'],str(len(PNG)));self.assertNotIn('download_url',r)
    def test_upload_invalid_files(self):
        for name,data,mime,code in [('../bad.png',PNG,'image/png','internal_filename_invalid'),('x.png',b'not png','image/png','internal_png_required'),('x.png',PNG,'image/jpeg','internal_png_required'),('x.png',PNG+b'x'*app.MAX_FILE,'image/png','internal_png_required')]:self.error(code,lambda:self.adapter.upload_attachment(name,data,mime,before_post=lambda:None))
    def test_comment_explicit_attachment_escaped_body(self):
        with self.session(Session(Response(current()),Response(comment(attached=True),status=201))) as s:r=self.adapter.create_comment(TID,'A < B',attachments=[attachment()],before_post=lambda:None)
        self.assertEqual(r,{'provider_id':'444'});data=json.loads(s.calls[-1][2]['data'])['content'];self.assertIn('A &lt; B',data);self.assertIn('<bc-attachment sgid="'+SGID+'">',data)
    def test_comment_scope_changed_blocks_post(self):
        row=current();row['visible_to_clients']=True
        with self.session(Session(Response(row))) as s:self.error('internal_task_scope_changed',lambda:self.adapter.create_comment(TID,'x',before_post=lambda:None))
        self.assertEqual([x[0] for x in s.calls],['GET'])
    def test_callback_stop_after_read_blocks_post(self):
        def stop():raise app.InternalProviderError('stopped')
        with self.session(Session(Response(current()))) as s:self.error('stopped',lambda:self.adapter.create_comment(TID,'x',before_post=stop))
        self.assertEqual(len(s.calls),1)
    def test_comment_readback_expanded_attachment_metadata(self):
        with self.session(Session(Response(current()),Response(comment(attached=True)))):r=self.adapter.read_comment('444',TID,'Test',[attachment()])
        self.assertTrue(r['matches_approved_payload']);self.assertEqual(r['attachment_count'],1)
    def test_comment_readback_changed_text_or_extra_attachment(self):
        for row,code in [(comment('Edited'),'comment_body_mismatch'),(comment(attached=True),'comment_attachments_mismatch')]:
            with self.session(Session(Response(current()),Response(row))):self.error(code,lambda:self.adapter.read_comment('444',TID,'Test'))
    def test_download_exact_bytes_known_basecamp_storage_auth(self):
        with self.session(Session(Response(current()),Response(comment(attached=True)),Response(PNG))) as s:self.assertEqual(self.adapter.download_attachment(TID,'444',attachment()),PNG)
        self.assertEqual(s.calls[-1][2]['headers']['Authorization'],'Bearer '+FAKE_TOKEN);self.assertFalse(s.calls[-1][2]['allow_redirects'])
    def test_download_rejects_ssrf_crossaccount(self):
        for url in ('http://127.0.0.1/x','https://storage.3.basecamp.com.evil.invalid/5898865/blobs/h/download/x','https://storage.3.basecamp.com/OTHER/blobs/h/download/x','https://user@storage.3.basecamp.com/5898865/blobs/h/download/x'):
            row=comment(attached=True);row['content_attachments'][0]['download_url']=url
            with self.session(Session(Response(current()),Response(row))) as s:self.error('attachment_download_url_invalid',lambda:self.adapter.download_attachment(TID,'444',attachment()))
            self.assertEqual(len(s.calls),2)
    def test_download_changed_bytes_or_redirect_no_retry(self):
        for response,code in [(Response(PNG+b'X'),'attachment_bytes_mismatch'),(Response({},status=302),'attachment_redirect_forbidden')]:
            with self.session(Session(Response(current()),Response(comment(attached=True)),response)) as s:self.error(code,lambda:self.adapter.download_attachment(TID,'444',attachment()))
            self.assertEqual(len(s.calls),3)
    def test_upload_timeout_one_attempt_redacted(self):
        with self.session(Session(TimeoutError(FAKE_TOKEN))) as s:self.error('provider_outcome_unknown_no_retry',lambda:self.adapter.upload_attachment('x.png',PNG,'image/png',before_post=lambda:None))
        self.assertEqual(len(s.calls),1)
    def test_delivery_two_pngs_same_thread_and_cc(self):
        with self.session(Session(Response({'id':'fed01','threadId':evidence()['thread_id']}))) as s:self.assertEqual(self.adapter.send_delivery(evidence(),'Merhaba',files(),before_post=lambda:None),{'provider_id':'fed01'})
        data=json.loads(s.calls[0][2]['data']);self.assertEqual(data['threadId'],evidence()['thread_id']);msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(data['raw']));self.assertEqual(str(msg['To']),app.provider.AHMET);self.assertEqual(str(msg['Cc']),'cansu@gamechangers.com.tr');self.assertEqual([p.get_filename() for p in msg.iter_attachments()],['stage-1.png','stage-2.png'])
    def test_delivery_no_historical_thread_or_customer_cc(self):
        row=evidence();row['thread_id']=app.provider.CONTINUATION_THREAD;self.error('historical_thread_forbidden',lambda:self.adapter._delivery(row,'Hi',files()))
        row=evidence();row['latest_cc']=['client@example.invalid']
        with self.assertRaises(Exception):self.adapter._delivery(row,'Hi',files())
    def test_delivery_requires_two_distinct_files(self):
        self.error('two_delivery_pngs_required',lambda:self.adapter._delivery(evidence(),'Hi',files()[:1]));rows=files();rows[1]['filename']=rows[0]['filename'];self.error('delivery_filename_duplicate',lambda:self.adapter._delivery(evidence(),'Hi',rows))
    def test_delivery_readback_all_attachment_hashes(self):
        raw,_,_=self.adapter._delivery(evidence(),'Hi',files());r={'id':'fed01','threadId':evidence()['thread_id'],'labelIds':['SENT'],'raw':base64.urlsafe_b64encode(raw).decode()}
        with self.session(Session(Response(r))):result=self.adapter.read_delivery('fed01',evidence(),'Hi',files())
        self.assertEqual(result['attachment_sha256'],[hashlib.sha256(x['data']).hexdigest() for x in files()])
    def test_delivery_readback_changed_body_file_or_bcc(self):
        raw,_,_=self.adapter._delivery(evidence(),'Hi',files())
        for kind,code in [('body','delivery_readback_body_mismatch'),('file','delivery_readback_attachment_mismatch'),('bcc','delivery_readback_header_mismatch')]:
            msg=BytesParser(policy=policy.default).parsebytes(raw)
            if kind=='body':msg.get_payload()[0].set_content('Changed')
            elif kind=='file':msg.get_payload()[1].set_payload(base64.b64encode(PNG+b'changed').decode())
            else:msg['Bcc']='client@example.invalid'
            r={'id':'fed01','threadId':evidence()['thread_id'],'labelIds':['SENT'],'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}
            with self.session(Session(Response(r))):self.error(code,lambda:self.adapter.read_delivery('fed01',evidence(),'Hi',files()))

    def test_upload_sgid_only_is_explicit_opaque_identity(self):
        with self.session(Session(Response({'attachable_sgid':SGID},status=201))):
            receipt=self.adapter.upload_attachment('stage-1.png',PNG,'image/png',before_post=lambda:None)
        self.assertEqual(receipt['identifier_kind'],'sgid_digest')
        self.assertEqual(receipt['provider_id'],'basecamp-sgid-sha256:'+hashlib.sha256(SGID.encode()).hexdigest())
        with self.session(Session(Response(current()),Response(comment(attached=True)))):
            proof=self.adapter.read_comment('444',TID,'Test',[receipt])
        self.assertEqual(proof['attachment_receipts'][0]['provider_id'],'333')
        self.assertEqual(proof['attachment_receipts'][0]['identifier_kind'],'native_id')
    def test_native_download_redirect_strips_authorization(self):
        row=comment(attached=True);row['content_attachments'][0]['download_url']='https://3.basecampapi.com/5898865/blobs/hash/download/stage-1.png'
        redirect=Response({},status=302,headers={'Location':'https://storage.basecamp.com/opaque?signature=synthetic'})
        with self.session(Session(Response(current()),Response(row),redirect,Response(PNG))) as s:
            self.assertEqual(self.adapter.download_attachment(TID,'444',attachment()),PNG)
        self.assertEqual(s.calls[-2][2]['headers']['Authorization'],'Bearer '+FAKE_TOKEN)
        self.assertNotIn('Authorization',s.calls[-1][2]['headers']);self.assertFalse(s.calls[-1][2]['allow_redirects'])
    def test_storage_redirect_bad_host_stops_without_extra_get(self):
        for host in ('127.0.0.1','launchpad.37signals.com','storage.basecamp.com.evil.invalid','user@storage.basecamp.com'):
            redirect=Response({},status=302,headers={'Location':'https://'+host+'/opaque'})
            with self.session(Session(Response(current()),Response(comment(attached=True)),redirect)) as s:
                self.error('attachment_redirect_forbidden',lambda:self.adapter.download_attachment(TID,'444',attachment()))
            self.assertEqual(len(s.calls),3)

if __name__=='__main__':unittest.main()
