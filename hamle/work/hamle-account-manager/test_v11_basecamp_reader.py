import copy
import unittest

from v11_basecamp_reader import BasecampReader, ReadError, SessionGet

ROOT = 'https://3.basecampapi.com/10'
TASK = ROOT + '/todos/40.json'
COMMENTS = ROOT + '/recordings/40/comments.json'
WORK = dict(work_id='synthetic-1', account_id=10, project_id=20, list_id=30, task_id=40)


def comment(cid=50, text='<div>İş hazır</div>'):
    return dict(id=cid, type='Comment', status='active', visible_to_clients=False,
                bucket={'id':20}, parent={'id':40}, creator={'id':60}, content=text)


def task(count=1):
    return dict(id=40, type='Todo', status='active', visible_to_clients=False,
                bucket={'id':20}, parent={'id':30}, completed=True,
                assignees=[{'id':60}], comments_count=count, comments_url=COMMENTS)


class FakeGet:
    def __init__(self):
        self.routes = {TASK: (200, task(), {'ETag':'task-1'}),
                       COMMENTS: (200, [comment()], {'ETag':'comments-1'})}
        self.reads = []

    def __call__(self, url, headers):
        self.reads.append((url, headers))
        status, body, response = self.routes[url]
        if headers.get('If-None-Match') == response.get('ETag'):
            return 304, None, {'ETag':response['ETag']}
        return copy.deepcopy((status, body, response))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.get = FakeGet()
        self.reader = BasecampReader(self.get)

    def test_completed_and_ready_comment_never_grant_delivery(self):
        r = self.reader.poll(WORK)
        self.assertEqual(len(r['changes']),2)
        self.assertTrue(r['complete'])
        self.assertFalse(r['delivery_authorized'])
        self.assertFalse(r['producer_release_verified'])
        self.assertFalse(r['quality_accepted'])
        self.assertFalse(r['artifact_bytes_verified'])
        self.assertEqual({u for u,_ in self.get.reads},{TASK,COMMENTS})

    def test_conditional_poll_unchanged_emits_no_events(self):
        one = self.reader.poll(WORK)
        two = self.reader.poll(WORK,previous=one['checkpoint'])
        self.assertEqual(two['changes'],[])
        self.assertEqual(one['checkpoint'],two['checkpoint'])
        self.assertTrue(all(h.get('If-None-Match') for _,h in self.get.reads[-3:]))

    def test_edit_to_old_comment_detected_without_new_id(self):
        one = self.reader.poll(WORK)
        self.get.routes[COMMENTS]=(200,[comment(text='new attachment reference')],{'ETag':'comments-2'})
        two = self.reader.poll(WORK,previous=one['checkpoint'])
        self.assertEqual([x['kind'] for x in two['changes']],['comment_observed'])
        self.assertGreater(two['changes'][0]['source_order'],one['checkpoint']['sequence'])

    def test_deleted_comment_observation_and_later_reappearance(self):
        one = self.reader.poll(WORK)
        self.get.routes[COMMENTS]=(200,[],{'ETag':'comments-2'})
        self.get.routes[TASK]=(200,task(0),{'ETag':'task-2'})
        two=self.reader.poll(WORK,previous=one['checkpoint'])
        self.assertEqual(two['changes'][-1]['kind'],'comment_absent')
        self.get.routes[COMMENTS]=(200,[comment()],{'ETag':'comments-3'})
        self.get.routes[TASK]=(200,task(),{'ETag':'task-3'})
        three=self.reader.poll(WORK,previous=two['checkpoint'])
        self.assertEqual(three['changes'][-1]['kind'],'comment_observed')
        self.assertNotEqual(one['changes'][-1]['event_id'],three['changes'][-1]['event_id'])

    def test_two_pages_follow_exact_next_link_and_cache_it(self):
        url=COMMENTS+'?page=2'
        self.get.routes[TASK]=(200,task(2),{'ETag':'task-2'})
        self.get.routes[COMMENTS]=(200,[comment()],{'ETag':'comments-1','Link':f'<{url}>; rel="next"'})
        self.get.routes[url]=(200,[comment(51)],{'ETag':'page2'})
        one=self.reader.poll(WORK)
        two=self.reader.poll(WORK,previous=one['checkpoint'])
        self.assertEqual(one['pages_read'],2)
        self.assertEqual(two['pages_read'],2)
        self.assertEqual(two['changes'],[])

    def test_foreign_next_url_blocked_before_transport(self):
        for url in ['https://evil.example/?secret=1',ROOT+'/recordings/99/comments.json?page=2',
                    'https://3.basecampapi.com.evil.example/10/recordings/40/comments.json?page=2']:
            with self.subTest(url=url):
                get=FakeGet()
                get.routes[COMMENTS][2]['Link']=f'<{url}>; rel="next"'
                with self.assertRaises(ReadError): BasecampReader(get).poll(WORK)
                self.assertNotIn(url,[u for u,_ in get.reads])

    def test_moved_task_or_wrong_project_is_rejected(self):
        for field in ['bucket','parent']:
            with self.subTest(field=field):
                get=FakeGet();get.routes[TASK][1][field]['id']=999
                with self.assertRaisesRegex(ReadError,'binding_mismatch'): BasecampReader(get).poll(WORK)

    def test_comment_wrong_parent_is_rejected(self):
        self.get.routes[COMMENTS][1][0]['parent']['id']=41
        with self.assertRaisesRegex(ReadError,'binding_mismatch'): self.reader.poll(WORK)

    def test_duplicate_comment_or_page_cycle_rejected(self):
        self.get.routes[COMMENTS][1].append(comment())
        with self.assertRaisesRegex(ReadError,'duplicate_comment'): self.reader.poll(WORK)
        self.get=FakeGet();self.get.routes[COMMENTS][2]['Link']=f'<{COMMENTS}>; rel="next"'
        with self.assertRaisesRegex(ReadError,'pagination_cycle'): BasecampReader(self.get).poll(WORK)

    def test_page_limit_never_returns_partial_success(self):
        self.get.routes[COMMENTS][2]['Link']=f'<{COMMENTS}?page=2>; rel="next"'
        with self.assertRaisesRegex(ReadError,'page_limit'): BasecampReader(self.get,max_pages=1).poll(WORK)

    def test_count_mismatch_never_returns_complete(self):
        self.get.routes[TASK][1]['comments_count']=2
        with self.assertRaisesRegex(ReadError,'changed_during_poll'): self.reader.poll(WORK)

    def test_task_changed_during_poll_rejected(self):
        def changing(url,headers):
            result=self.get(url,headers)
            if url==COMMENTS:self.get.routes[TASK]=(200,task(2),{'ETag':'changed'})
            return result
        with self.assertRaisesRegex(ReadError,'changed_during_poll'): BasecampReader(changing).poll(WORK)

    def test_foreign_work_checkpoint_cannot_be_reused(self):
        r=self.reader.poll(WORK)
        r['checkpoint']['binding']['work_id']='other'
        with self.assertRaisesRegex(ReadError,'checkpoint_binding_mismatch'):
            self.reader.poll(WORK,previous=r['checkpoint'])

    def test_crash_before_checkpoint_commit_replays_same_event_ids(self):
        first=self.reader.poll(WORK)
        second=self.reader.poll(WORK)
        self.assertEqual(first['changes'],second['changes'])

    def test_304_without_cache_is_rejected(self):
        with self.assertRaisesRegex(ReadError,'304_without_cache'):
            BasecampReader(lambda *_:(304,None,{})).poll(WORK)

    def test_replayed_headers_never_authorization_or_cookie(self):
        self.reader.poll(WORK); self.reader.poll(WORK)
        self.assertTrue(all(set(h)<= {'If-None-Match','If-Modified-Since'} for _,h in self.get.reads))

    def test_transport_failure_does_not_reveal_provider_text(self):
        def bad(*args):raise RuntimeError('secret credential')
        with self.assertRaisesRegex(ReadError,'^basecamp_transport_failed$'): BasecampReader(bad).poll(WORK)

    def test_blank_extra_query_cannot_bypass_allowlist(self):
        for query in ['?page=1&other=','?page=','?page=2&page=3','?x','?page=1&next=2']:
            with self.subTest(query=query):
                get=FakeGet();get.routes[TASK][1]['comments_url']=COMMENTS+query
                with self.assertRaises(ReadError): BasecampReader(get).poll(WORK)
                self.assertEqual(len(get.reads),1)

    def test_transport_rejects_foreign_url_before_loading_token(self):
        loaded=[]
        transport=SessionGet(None,lambda:loaded.append('loaded'))
        for url in ['https://evil.example/10/todos/40.json', TASK+'#fragment',
                    'http://3.basecampapi.com/10/todos/40.json', ROOT+'/people.json',TASK+'?page=']:
            with self.assertRaises(ReadError):transport(url,{})
        self.assertEqual(loaded,[])

    def test_transport_headers_cannot_override_authorization(self):
        loaded=[]
        transport=SessionGet(None,lambda:loaded.append('loaded'))
        for headers in [{'Authorization':'Bearer bad'},{'Cookie':'bad'},{'If-None-Match':'bad\r\nOther: bad'}]:
            with self.assertRaises(ReadError):transport(TASK,headers)
        self.assertEqual(loaded,[])

    def test_malformed_checkpoint_refused_before_any_read(self):
        previous=self.reader.poll(WORK)['checkpoint']
        for update in [{'comments':{'invalid':'x'}},{'comments':None},{'sequence':True}]:
            broken=copy.deepcopy(previous);broken.update(update)
            count=len(self.get.reads)
            with self.assertRaises(ReadError):self.reader.poll(WORK,previous=broken)
            self.assertEqual(len(self.get.reads),count)

    def test_null_bucket_or_parent_is_sanitized(self):
        for key in ['bucket','parent']:
            get=FakeGet();get.routes[TASK][1][key]=None
            with self.assertRaisesRegex(ReadError,'binding_mismatch'):BasecampReader(get).poll(WORK)


if __name__=='__main__': unittest.main()
