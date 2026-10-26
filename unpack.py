#!/usr/bin/env python3
"""minigit_packed.py -> 元のソースに戻す (pack.py の逆)
  使い方: python unpack.py [入力 既定 minigit_packed.py] [-f]
  - v2 バンドル: minigit.py と minigit_<lang>.json 一式を各 SHA256 で照合しながら復元
  - v1 旧パック (単一 minigit.py): 埋め込み SHA256 と照合しバイト単位で完全復元。bz2/xz/lzma をマジックで自動判別
  - 既存ファイルは上書きしない (-f で上書き)
"""
import base64, bz2, hashlib, io, lzma, os, re, sys, tarfile

args = [a for a in sys.argv[1:] if a != "-f"]
force = "-f" in sys.argv[1:]
src = args[0] if args else "minigit_packed.py"

text = open(src, encoding="ascii").read()
m = re.search(r'"""(.*?)"""', text, re.S)
if not m or "b85decode" not in text:
    sys.exit(f"{src} は pack.py で作った形式ではありません")
blob = base64.b85decode("".join(m.group(1).split()))

def decomp(blob):  # 先頭マジックで bz2 / xz-lzma / raw-lzma を判別
    if blob[:3] == b"BZh":
        return bz2.decompress(blob)
    if blob[:6] == b"\xfd7zXZ\x00":
        return lzma.decompress(blob)
    return lzma.decompress(blob, format=lzma.FORMAT_RAW,
                           filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}])

try:
    raw = decomp(blob)
except (ValueError, EOFError, OSError, lzma.LZMAError) as ex:
    sys.exit(f"{src} のデータが壊れています (転送や結合の失敗の可能性): {ex}")

def place(name, data, want):
    got = hashlib.sha256(data).hexdigest()
    if want and want != got:
        sys.exit(f"{name} SHA256 不一致: 埋め込み {want} / 展開結果 {got}")
    if os.path.exists(name) and not force:
        if open(name, "rb").read() == data:
            print(f"{name} は既に同じ内容です")
            return
        sys.exit(f"{name} は既に存在し内容が異なります。上書きする場合は -f を付けてください")
    with open(name, "wb") as f:
        f.write(data)
    print(f"{name} ({len(data)} bytes)  sha256 {got}  OK")

if "bundle v2" in text[:600]:
    want = dict(re.findall(r"^# file (\S+) sha256 ([0-9a-f]{64})$", text, re.M))
    tf = tarfile.open(fileobj=io.BytesIO(raw))
    members = tf.getmembers()
    if not members:
        sys.exit(f"{src} のバンドルが空です")
    for mem in members:
        place(mem.name, tf.extractfile(mem).read(), want.get(mem.name))
else:
    h = re.search(r"sha256: ([0-9a-f]{64})", text)
    place(args[1] if len(args) > 1 else "minigit.py", raw, h.group(1) if h else None)
