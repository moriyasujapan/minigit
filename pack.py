#!/usr/bin/env python3
"""minigit ソース一式 -> minigit_packed.py (bz2 + base85 の自己展開バンドル)
  使い方: python3 pack.py [出力 既定 minigit_packed.py] [パックするファイル... 既定 minigit.py + minigit_<lang>.json の存在分]
  - i18n 以降、実行には minigit.py と minigit_<lang>.json が必要なので全ファイルを 1 つのバンドルに pack する
  - packed は自分の隣に各ファイルを書き出して内蔵の minigit.py を起動する (既に存在するファイルは上書きしない)
  - python unpack.py で全ファイルに完全復元 (ファイルごとに SHA256 照合)
"""
import base64, bz2, hashlib, io, os, sys, tarfile

args = sys.argv[1:]
dst = args[0] if args else "minigit_packed.py"
names = args[1:] or ["minigit.py", "minigit_ja.json", "minigit_zh.json", "minigit_en.json"]
files = [f for f in names if os.path.isfile(f)]
if "minigit.py" not in files:
    sys.exit("minigit.py がパック対象にありません")
compile(open("minigit.py", "rb").read(), "minigit.py", "exec")  # 構文エラーのあるソースはパックしない

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as tf:  # tar 格納 (無圧縮) + 全体を bz2 -9
    for f in files:
        raw = open(f, "rb").read()
        ti = tarfile.TarInfo(os.path.basename(f))
        ti.size, ti.mtime, ti.mode = len(raw), 0, 0o644
        tf.addfile(ti, io.BytesIO(raw))
blob = base64.b85encode(bz2.compress(buf.getvalue(), 9)).decode()
meta = "".join("# file %s sha256 %s\n" % (os.path.basename(f),
           hashlib.sha256(open(f, "rb").read()).hexdigest()) for f in files)
packed = (
    "#!/usr/bin/env python3\n"
    f"# minigit bundle v2 (bz2+base85 self-extracting). usage: python {os.path.basename(dst)} 8080\n"
    f"# files: {' '.join(os.path.basename(f) for f in files)}\n"
    + meta +
    "# restore the original sources: python unpack.py\n"
    "import base64,bz2,io,os,tarfile\n"
    'DATA=bz2.decompress(base64.b85decode("".join("""\n'
    + "\n".join(blob[i:i + 100] for i in range(0, len(blob), 100))
    + '\n""".split())))\n'
    "here=os.path.dirname(os.path.abspath(__file__))\n"
    "tf=tarfile.open(fileobj=io.BytesIO(DATA)); src=None\n"
    "for m in tf:\n"
    "    raw=tf.extractfile(m).read(); p=os.path.join(here,m.name)\n"
    "    if m.name=='minigit.py': src=raw\n"
    "    if not os.path.exists(p): open(p,'wb').write(raw); print('extracted',m.name)\n"
    "    elif open(p,'rb').read()!=raw: print('existing file kept (bundled code runs)',m.name)\n"
    'exec(compile(src.decode("utf-8"),"minigit.py","exec"),'
    '{"__name__":"__main__","__file__":os.path.join(here,"minigit.py")})\n'
)
open(dst, "w", encoding="ascii", newline="\n").write(packed)
print(f"bundle {len(files)} files -> {dst} ({len(packed)} bytes)")
