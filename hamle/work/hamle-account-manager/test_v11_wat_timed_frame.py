import copy,hashlib,json,socket,struct,unittest
from unittest.mock import patch
import v11_wat_timed_frame as app

MP4=b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2'+b'synthetic'
PNG=b'\x89PNG\r\n\x1a\nsynthetic'
SHA=hashlib.sha256(MP4).hexdigest()


def probe(timestamps=(0,500,1500,1980,2020,2500)):
    return {'streams':[{'codec_type':'video','codec_name':'h264','width':1080,'height':1920,'time_base':'1/1000'}],
        'format':{'format_name':'mov,mp4','duration':'3','start_time':'0'},
        'frames':[{'media_type':'video','best_effort_timestamp':n} for n in timestamps]}


def packed(pts='1980',base='1/1000',data=PNG):
    header=json.dumps({'showinfo_pts':pts,'showinfo_time_base':base}).encode()
    return struct.pack('>I',len(header))+header+data


class TimedFrameTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket','create_connection'):
            p=patch.object(socket,name,side_effect=AssertionError('No network'));p.start();self.addCleanup(p.stop)

    def test_variable_pts_selects_before_two_seconds_not_nearest_after(self):
        row=app.select_deadline_frame(probe())
        self.assertEqual(row['frame_index'],3);self.assertEqual(row['elapsed_seconds'],'99/50')

    def test_exact_boundary_is_allowed(self):
        row=app.select_deadline_frame(probe((0,1000,2000,2001)))
        self.assertEqual(row['frame_index'],2);self.assertEqual(row['elapsed_seconds'],'2')

    def test_container_origin_is_preserved(self):
        p=probe((100,1100,2080,2120));p['format']['start_time']='0.1'
        row=app.select_deadline_frame(p);self.assertEqual(row['frame_index'],2);self.assertEqual(row['elapsed_seconds'],'99/50')

    def test_unknown_or_invalid_pts_holds(self):
        cases=[]
        p=probe();del p['format']['start_time'];cases.append(p)
        p=probe();del p['frames'][1]['best_effort_timestamp'];cases.append(p)
        cases += [probe((0,1000,999)),probe((0,1000,1000)),probe((2100,2200))]
        for p in cases:
            with self.assertRaises(app.TimedFrameError):app.select_deadline_frame(p)

    def test_bounds_and_missing_duration(self):
        for duration in ('0','61','NaN',None):
            p=probe();p['format']['duration']=duration
            with self.assertRaises(app.TimedFrameError):app.select_deadline_frame(p)
        p=probe(tuple(range(3601)))
        with self.assertRaises(app.TimedFrameError):app.select_deadline_frame(p)

    def test_real_pts_crosscheck_and_png_hash(self):
        with patch.object(app,'_run',side_effect=[json.dumps(probe()).encode(),packed()]) as runner:
            result=app.inspect_deadline_frame(MP4,expected_sha256=SHA,include_frame_bytes=True)
        self.assertEqual(result['status'],'verified_timestamp_frame');self.assertEqual(result['frame_data'],PNG)
        self.assertEqual(result['frame']['sha256'],hashlib.sha256(PNG).hexdigest())
        self.assertTrue(result['frame']['timestamp_verified']);self.assertFalse(result['message_presence_verified'])
        self.assertFalse(result['absence_is_failure']);self.assertFalse(result['delivery_authorized'])
        self.assertEqual(runner.call_args.args[-1],3)

    def test_different_pts_or_png_signature_returns_held_not_false_fail(self):
        for packet in (packed('2020'),packed(data=b'bad'),b'broken'):
            with patch.object(app,'_run',side_effect=[json.dumps(probe()).encode(),packet]):
                result=app.inspect_deadline_frame(MP4,expected_sha256=SHA,include_frame_bytes=True)
            self.assertEqual(result['status'],'held');self.assertNotIn('frame_data',result)
            self.assertFalse(result['message_presence_verified']);self.assertFalse(result['absence_is_failure'])

    def test_rescaled_decode_timebase_compares_exact_rational_value(self):
        with patch.object(app,'_run',side_effect=[json.dumps(probe()).encode(),packed('99','1/50')]):
            result=app.inspect_deadline_frame(MP4,expected_sha256=SHA)
        self.assertEqual(result['status'],'verified_timestamp_frame')

    def test_wrong_artifact_hash_stops_before_tool(self):
        with patch.object(app,'_run',side_effect=AssertionError('must not decode')):
            with self.assertRaises(app.TimedFrameError):app.inspect_deadline_frame(MP4,expected_sha256='0'*64)

    def test_no_raw_bytes_in_default_json(self):
        with patch.object(app,'_run',side_effect=[json.dumps(probe()).encode(),packed()]):
            r=app.inspect_deadline_frame(MP4,expected_sha256=SHA)
        self.assertNotIn('frame_data',r);json.dumps(r)

    def test_unavailable_tool_is_held_without_retry(self):
        with patch.object(app,'_run',side_effect=app.TimedFrameError('timing_parser_failed_or_limited')) as run:
            result=app.inspect_deadline_frame(MP4,expected_sha256=SHA)
        self.assertEqual(result['status'],'held');self.assertEqual(run.call_count,1)


if __name__=='__main__':unittest.main()
