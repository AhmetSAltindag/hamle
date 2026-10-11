"""Read exactly the user-selected current WAT asset; no business writes."""
from datetime import datetime,timezone
import hashlib,json,os,re,subprocess,sys
from pathlib import Path
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parent/'hamle-account-manager'))
import cloud_providers
from verify_wat_read_only import ENV,CLI,ROOT,read
from read_wat_example_read_only import content
OUT=ROOT/'outputs/wat-pilot-20261010/current-brand-assets-10392828700'
ACCOUNT=5898865;PROJECT=40694338;UPLOAD=10392828700

def token():
 p=subprocess.run([str(CLI),'auth','token','--stored','--account',str(ACCOUNT)],env=ENV,capture_output=True,text=True,timeout=30)
 if p.returncode:raise RuntimeError('auth_unavailable')
 return cloud_providers._token(p.stdout.strip())
def main():
 os.umask(0o077);OUT.mkdir(parents=True,exist_ok=True)
 identity=read(['auth','status','--check']);d=identity.get('data',{})
 if not(identity['success'] and d.get('valid') is True and d.get('user_email')=='hamle@gamechangers.com.tr' and str(d.get('account_id'))==str(ACCOUNT)):raise RuntimeError('identity_unverified')
 access=token();s=cloud_providers._session()
 try:
  raw,_=cloud_providers._request(s,'GET',f'https://3.basecampapi.com/{ACCOUNT}/buckets/{PROJECT}/uploads/{UPLOAD}.json',headers={'Authorization':'Bearer '+access,'User-Agent':'Hamle current approved WAT asset read'})
  row=cloud_providers._json(raw)
  if row.get('id')!=UPLOAD or row.get('bucket',{}).get('id')!=PROJECT:raise RuntimeError('upload_binding_mismatch')
  result={'read_at':datetime.now(timezone.utc).isoformat(),'source_user_answer_current_approved':True,'approval_scope':'Only user-linked upload10392828700; not all WAT folder files','user_source_url':f'https://app.basecamp.com/{ACCOUNT}/buckets/{PROJECT}/uploads/{UPLOAD}',
    'metadata':{k:row.get(k) for k in ('id','title','filename','content_type','byte_size','width','height','status','visible_to_clients','created_at','updated_at','version','version_count')},
    'description':content(row.get('description')),'creator':{k:row.get('creator',{}).get(k) for k in ('id','name')},
    'parent':{k:row.get('parent',{}).get(k) for k in ('id','title','type')},'metadata_field_names':list(row),'files_downloaded':0,'business_writes':0,'paid_model_calls':0,'cloud_iam_changes':0,'signed_urls_recorded':False}
  path=OUT/'metadata.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
  if '--download' in sys.argv:
   expected=row.get('byte_size');name=row.get('filename');url=row.get('download_url');u=urlsplit(url or '')
   if type(expected)!=int or not(0<expected<=64*1024*1024):raise RuntimeError('asset_size_not_bounded')
   if not(isinstance(name,str) and 0<len(name)<200 and Path(name).name==name and '/' not in name and '\\' not in name):raise RuntimeError('asset_filename_invalid')
   if not(u.scheme=='https' and u.netloc in ('3.basecampapi.com','storage.3.basecamp.com','storage.app.basecamp.com') and (u.path.startswith(f'/{ACCOUNT}/blobs/') or u.path.startswith(f'/{ACCOUNT}/buckets/{PROJECT}/uploads/{UPLOAD}/download/')) and '/download/' in u.path and not u.fragment):raise RuntimeError('asset_download_url_invalid')
   response=s.request('GET',url,headers={'Authorization':'Bearer '+access},timeout=(10,30),allow_redirects=False,stream=True)
   if response.status_code==302:
    location=response.headers.get('Location','');redirect=urlsplit(location);response.close()
    if redirect.scheme!='https' or redirect.netloc!='storage.basecamp.com' or redirect.fragment:raise RuntimeError('asset_redirect_unverified')
    response=s.request('GET',location,timeout=(10,30),allow_redirects=False,stream=True)
   try:
    if response.status_code!=200:raise RuntimeError('asset_download_failed')
    parts=[];total=0
    for chunk in response.iter_content(16384):
     total+=len(chunk)
     if total>expected:raise RuntimeError('asset_size_changed')
     parts.append(chunk)
    data=b''.join(parts)
   finally:response.close()
   if len(data)!=expected:raise RuntimeError('asset_size_changed')
   target=OUT/name
   if target.exists() and target.read_bytes()!=data:raise RuntimeError('asset_version_conflict')
   target.write_bytes(data);target.chmod(0o600)
   result.update(files_downloaded=1,file={'name':name,'path':str(target),'byte_size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'magic_hex':data[:12].hex()})
   path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps(result,ensure_ascii=False,indent=2))
 finally:s.close()
if __name__=='__main__':
 try:main()
 except Exception as e:
  print(json.dumps({'status':'held','reason':str(e) if type(e) is RuntimeError else type(e).__name__,'secret_printed':False}));raise SystemExit(2)
