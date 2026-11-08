from pathlib import Path
import hashlib,json
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'SOURCE_MANIFEST.json').read_text())
for row in manifest['files']:
    path=root/row['path']
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],row['path']
print(f"Verified {len(manifest['files'])} delivered source files.")
