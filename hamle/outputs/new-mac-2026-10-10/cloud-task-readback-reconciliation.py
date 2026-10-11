import hashlib,inspect
import business_writers as w
original=w._PlainDescription.handle_starttag
if hashlib.sha256(inspect.getsource(original).encode()).hexdigest() != '90f211d14ab2a4455ac1ed25941dd9fdd1051e47aae3487ab90ab6780461c986':
 raise SystemExit("unexpected_writer_source")
def normalize_basecamp_dir(self,tag,attrs):
 if tag == "div" and attrs == [("dir","auto")]:
  attrs=[]
 return original(self,tag,attrs)
w._PlainDescription.handle_starttag=normalize_basecamp_dir
from worker import main
raise SystemExit(main(["--mode","dispatch","--proposal-id","p-a0627d6de33573f9284e8799b2b537af6da59d22bdfcff812158e64f3490067d"]))
