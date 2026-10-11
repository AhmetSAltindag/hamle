import copy
import hashlib
import json
import socket
import unittest
from unittest.mock import patch

import v11_wat_readonly as app
from test_cloud_providers import Session, Response, FAKE_TOKEN

PNG = b'\x89PNG\r\n\x1a\nread-only-synthetic-test'
JPEG = b'\xff\xd8\xffsynthetic-jpeg\xff\xd9'
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00synthetic-video'
TID = 10309337809
CID = 10317954872
AID = 895203774
PID = 47658175
URL = 'https://3.basecampapi.com/5898865/blobs/opaque/download/file.png'


def profile():
    return {'id':53358100,'email_address':'hamle@gamechangers.com.tr'}


def task(tid=TID):
    return {'id':tid,'type':'Todo','status':'active','visible_to_clients':False,
            'bucket':{'id':app.PROJECT},'completed':True}


def comment(data=PNG, mime='image/png', tid=TID, pid=PID):
    return {'id':CID,'type':'Comment','status':'active','visible_to_clients':False,
            'bucket':{'id':app.PROJECT},'parent':{'id':tid,'type':'Todo'},'creator':{'id':pid},
            'content_attachments':[{'id':AID,'filename':'same-name.png','content_type':mime,
              'byte_size':len(data),'width':1080,'height':1350,'download_url':URL,'sgid':'never-return-this',
              'preview_url':'https://storage.basecamp.com/opaque?secret=never-print'}]}


class WATReadOnlyTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket','create_connection'):
            p=patch.object(socket,name,side_effect=AssertionError('Network forbidden in tests'));p.start();self.addCleanup(p.stop)
        self.manager=object();self.claim=lambda x:True
        self.reader=app.WATReadOnly(self.manager,self.claim)
        p=patch.object(app.basecamp_cloud_auth,'access_token',return_value=FAKE_TOKEN)
        self.auth=p.start();self.addCleanup(p.stop)
        self.addCleanup(self.reader.close)

    def run_read(self, responses, *, data=PNG, tid=TID, pid=PID, sha=None, size=None):
        session=Session(*responses)
        with patch.object(app.provider,'_session',return_value=session):
            output=self.reader.download(tid,CID,AID,sha or hashlib.sha256(data).hexdigest(),
                len(data) if size is None else size,pid)
        return output,session

    def error(self, code, call):
        with self.assertRaises(app.WATReadError) as got:call()
        self.assertEqual(got.exception.code,code)
        self.assertFalse(got.exception.retry_allowed)
        self.assertNotIn(FAKE_TOKEN,str(got.exception))
        self.assertNotIn('secret=',str(got.exception))

    def base(self, data=PNG, mime='image/png', tid=TID, pid=PID):
        return [Response(profile()),Response(task(tid)),Response(comment(data,mime,tid,pid))]

    def test_constructor_performs_no_network_or_secret_read(self):
        with patch.object(app.provider,'_session',side_effect=AssertionError('constructor network')):
            app.WATReadOnly(object()).close()
        self.auth.assert_not_called()

    def test_known_png_exact_bytes_get_only_and_existing_auth(self):
        out,s=self.run_read(self.base()+[Response(PNG)])
        self.assertEqual(out,PNG)
        self.assertEqual([c[0] for c in s.calls],['GET']*4)
        self.assertEqual([c[1] for c in s.calls], [app.PROFILE,app.BASE+f'/todos/{TID}.json',app.BASE+f'/comments/{CID}.json',URL])
        self.auth.assert_called_once_with(self.manager,claim_refresh=self.claim)
        self.assertTrue(all(c[2]['allow_redirects'] is False for c in s.calls))

    def test_jpeg_allowed_with_exact_signature_hash(self):
        out,_=self.run_read(self.base(JPEG,'image/jpeg')+[Response(JPEG)],data=JPEG)
        self.assertEqual(out,JPEG)

    def test_invalid_scope_or_expectation_fails_before_auth(self):
        cases=[('wat_task_not_authorized',(99,CID,AID,'a'*64,len(PNG),PID)),
            ('wat_producer_not_authorized',(TID,CID,AID,'a'*64,len(PNG),51207148)),
            ('wat_identifier_invalid',(True,CID,AID,'a'*64,len(PNG),PID)),
            ('wat_expected_hash_required',(TID,CID,AID,'bad',len(PNG),PID)),
            ('wat_expected_size_invalid',(TID,CID,AID,'a'*64,app.MAX_IMAGE+1,PID)),
            ('wat_expected_size_invalid',(TID,CID,AID,'a'*64,True,PID))]
        for code,args in cases:self.error(code,lambda:self.reader.download(*args))
        self.auth.assert_not_called()

    def test_wrong_runtime_identity_stops_before_task(self):
        self.error('wat_identity_mismatch',lambda:self.run_read([Response({'id':47470759,'email_address':'ahmet@gamechangers.com.tr'})]))

    def test_task_binding_guards(self):
        for key,value in [('id',99),('type','Message'),('bucket',{'id':49184053}),('status','trashed'),('visible_to_clients',True)]:
            row=task();row[key]=value;self.reader._session=None
            self.error('wat_task_binding_changed',lambda:self.run_read([Response(profile()),Response(row)]))

    def test_comment_binding_guards(self):
        for key,value in [('id',99),('type','Message'),('bucket',{'id':49184053}),('parent',{'id':99,'type':'Todo'}),('creator',{'id':50486696}),('status','trashed'),('visible_to_clients',True)]:
            row=comment();row[key]=value;self.reader._session=None
            self.error('wat_comment_binding_changed',lambda:self.run_read([Response(profile()),Response(task()),Response(row)]))

    def test_manifest_rejects_duplicate_native_id(self):
        row=comment();row['content_attachments']*=2
        self.error('wat_attachment_manifest_ambiguous',lambda:self.run_read([Response(profile()),Response(task()),Response(row)]))

    def test_same_filename_different_attachment_id_not_selected(self):
        row=comment();row['content_attachments'][0]['id']+=1
        self.error('wat_attachment_not_in_comment',lambda:self.run_read([Response(profile()),Response(task()),Response(row)]))

    def test_manifest_returns_safe_whitelist_only(self):
        s=Session(*self.base())
        with patch.object(app.provider,'_session',return_value=s):r=self.reader.manifest(TID,CID,PID)
        self.assertFalse(r['action_eligible']);self.assertEqual(r['attachments'][0]['id'],AID)
        self.assertNotIn('download_url',json.dumps(r));self.assertNotIn('sgid',json.dumps(r));self.assertNotIn('never-print',json.dumps(r))

    def test_wrong_type_or_metadata_size_holds(self):
        for change,code in [({'content_type':'application/pdf'},'wat_attachment_type_not_allowed'),({'byte_size':999},'wat_attachment_size_changed')]:
            row=comment();row['content_attachments'][0].update(change);self.reader._session=None
            self.error(code,lambda:self.run_read([Response(profile()),Response(task()),Response(row)]))

    def test_scope_urls_reject_before_downloading(self):
        for url in ['http://127.0.0.1/x','https://3.basecampapi.com.evil.invalid/5898865/blobs/x/download/y',
                    'https://3.basecampapi.com/99/blobs/x/download/y','https://u@3.basecampapi.com/5898865/blobs/x/download/y',
                    'https://3.basecampapi.com/5898865/blobs/x/../download/y','https://3.basecampapi.com/5898865/blobs/x/%2e%2e/download/y',
                    'https://3.basecampapi.com/5898865/blobs/x/download/y#fragment']:
            row=comment();row['content_attachments'][0]['download_url']=url;self.reader._session=None
            self.error('wat_download_url_invalid',lambda:self.run_read([Response(profile()),Response(task()),Response(row)]))

    def test_one_storage_redirect_without_auth(self):
        out,s=self.run_read(self.base()+[Response({},302,{'Location':'https://storage.basecamp.com/opaque?signature=test'}),Response(PNG)])
        self.assertEqual(out,PNG);self.assertNotIn('Authorization',s.calls[-1][2]['headers'])
        self.assertEqual(s.calls[-2][2]['headers']['Authorization'],'Bearer '+FAKE_TOKEN)

    def test_bad_redirect_or_second_redirect_holds(self):
        for target in ['https://evil.invalid/download','https://user@storage.basecamp.com/blob','http://storage.basecamp.com/blob']:
            self.reader._session=None
            self.error('wat_storage_redirect_invalid',lambda:self.run_read(self.base()+[Response({},302,{'Location':target})]))
        self.reader._session=None
        self.error('wat_read_failed_no_retry',lambda:self.run_read(self.base()+[Response({},302,{'Location':'https://storage.basecamp.com/one'}),Response({},302,{'Location':'https://storage.basecamp.com/two'})]))

    def test_bytes_size_hash_and_magic_are_all_required(self):
        for data,sha,code in [(PNG+b'x',None,'wat_response_too_large'),(PNG[:-1],None,'wat_attachment_size_changed'),(PNG,'0'*64,'wat_attachment_hash_changed')]:
            self.reader._session=None
            self.error(code,lambda:self.run_read(self.base()+[Response(data)],sha=sha))
        bad=b'x'*len(PNG);self.reader._session=None
        self.error('wat_attachment_signature_invalid',lambda:self.run_read(self.base()+[Response(bad)],data=bad))

    def test_network_failure_single_attempt_redacted(self):
        s=Session(Response(profile()),TimeoutError(FAKE_TOKEN+' secret=signed'))
        with patch.object(app.provider,'_session',return_value=s):
            self.error('wat_read_failed_no_retry',lambda:self.reader.download(TID,CID,AID,hashlib.sha256(PNG).hexdigest(),len(PNG),PID))
        self.assertEqual(len(s.calls),2)

    def test_refresh_hold_is_propagated_without_business_request(self):
        self.auth.side_effect=app.basecamp_cloud_auth.AuthError('durable_refresh_claim_required')
        self.error('durable_refresh_claim_required',lambda:self.reader.download(TID,CID,AID,'a'*64,len(PNG),PID))

    def test_samet_video_only_explicit_user_task(self):
        out,s=self.run_read(self.base(MP4,'video/mp4',10317695863,50486696)+[Response(MP4)],data=MP4,tid=10317695863,pid=50486696)
        self.assertEqual(out,MP4);self.assertTrue(all(x[0]=='GET' for x in s.calls))
        self.reader._session=None
        self.error('wat_attachment_type_not_allowed',lambda:self.run_read(self.base(MP4,'video/mp4')+[Response(MP4)],data=MP4))

    def test_samet_image_does_not_inherit_video_limit(self):
        row=comment(tid=10317695863,pid=50486696);row['content_attachments'][0]['byte_size']=app.MAX_IMAGE+1
        self.error('wat_expected_size_invalid',lambda:self.run_read([Response(profile()),Response(task(10317695863)),Response(row)],tid=10317695863,pid=50486696,size=app.MAX_IMAGE+1))

    def test_close_blocks_reuse_and_closes_session(self):
        _,s=self.run_read(self.base()+[Response(PNG)]);self.reader.close();self.assertTrue(s.closed)
        self.error('wat_reader_closed',lambda:self.reader.download(TID,CID,AID,'a'*64,len(PNG),PID))

    def test_guide_is_exact_upload_and_never_arbitrary_pdf(self):
        row={'id':app.GUIDE_UPLOAD,'bucket':{'id':app.PROJECT},'creator':{'id':47470759},'status':'active',
            'visible_to_clients':False,'content_type':'application/pdf','byte_size':app.GUIDE_SIZE,
            'filename':'WATMobilite-Brand Guideline.pdf','download_url':'https://3.basecampapi.com/secret'}
        s=Session(Response(profile()),Response(row))
        with patch.object(app.provider,'_session',return_value=s):result=self.reader.guide_metadata()
        self.assertEqual(result['id'],app.GUIDE_UPLOAD);self.assertNotIn('download_url',result)
        self.assertEqual(s.calls[-1][1],app.BASE+f'/uploads/{app.GUIDE_UPLOAD}.json')
        self.reader._session=None;row['byte_size']+=1;s=Session(Response(profile()),Response(row))
        with patch.object(app.provider,'_session',return_value=s):self.error('wat_approved_guide_changed',self.reader.guide_metadata)


if __name__=='__main__':unittest.main()
