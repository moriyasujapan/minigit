#!/usr/bin/env python3
"""extract.py — codemod: minigit.py の日本語文字列定数を t('sNNN') に置換し、minigit_ja.json と keys.json を出す。
使い方: python3 extract.py [minigit.py]   (同じディレクトリに minigit.py.bak を作ってから書き換える)
スキップ: docstring / コメント / re.* パターン内 / SIMPLE_CSS,APP_CSS 定数 / langbar() (言語名は各言語表記のまま)
設計: 日本語連結部のみキー化し、HTML タグ・引用符・改行などの構造はコード側に残す (翻訳単位を小さく保つ)。
f-string 後続の異種連結で JoinedStr に畳み込まれた通常文字列は、中括いがリテラル表記のままになるため
fold_end で連結域を再トークン化し、中括いをエスケープした新しい f-string リテラル 1 本へ書き換える。"""
import ast
import json
import re
import shutil
import sys

JA_RUN = re.compile(u"[\u3000-\u30ff\u4e00-\u9fff\uff01-\uff60\u2018\u2019\u201c\u201d\u3001\u3002\u300c\u300d"
                    u"\u3010\u3011\uff08\uff09\u30fc\u301c\u2026\u2015\u2010]+")
SRC = sys.argv[1] if len(sys.argv) > 1 else "minigit.py"
src = open(SRC, encoding="utf-8").read()
srcb = src.encode("utf-8")
tree = ast.parse(src)

offs = [0]
for ln in srcb.splitlines(keepends=True):
    offs.append(offs[-1] + len(ln))


def off(line, col):
    return offs[line - 1] + col


par = {}
for n in ast.walk(tree):
    for c in ast.iter_child_nodes(n):
        par[id(c)] = n

SKIP_CSS = set()
for st in tree.body:
    if isinstance(st, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id in ("SIMPLE_CSS", "APP_CSS")
                                          for tg in st.targets):
        for sub in ast.walk(st.value):
            SKIP_CSS.add(id(sub))


def skip_reason(n):
    if id(n) in SKIP_CSS:
        return "css"
    f = n
    while f is not None:
        if isinstance(f, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and f.body and isinstance(f.body[0], ast.Expr) and isinstance(f.body[0].value, ast.Constant) \
                and f.body[0].value is n:
            return "docstring"
        if isinstance(f, ast.FunctionDef) and f.name == "langbar":
            return "langbar"
        if isinstance(f, ast.Call) and isinstance(f.func, ast.Attribute) and isinstance(f.func.value, ast.Name) \
                and f.func.value.id == "re" and f.func.attr in (
                "compile", "match", "search", "sub", "subn", "fullmatch", "findall", "finditer", "split"):
            return "re." + f.func.attr
        f = par.get(id(f))
    return ""


keys = {}   # 原文 -> キー (重複排除)


def key(v):
    if v not in keys:
        keys[v] = "s%03d" % (len(keys) + 1)
    return keys[v]


def runs(text):
    """text を [('l'|'ja', seg)] に分割。日本語連結だけ分離し、他 ('l') は構造文字列のまま残す"""
    out, pos = [], 0
    for m in JA_RUN.finditer(text):
        if m.start() > pos:
            out.append(("l", text[pos:m.start()]))
        out.append(("ja", m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append(("l", text[pos:]))
    return out


repl = []      # (bstart, bend, 置換テキスト) 逆順で適用
modlevel = []  # 関数/クラスタ外 (モジュール直下) で置換した位置 = import 時固定の疑い、人手レビュー
warns = []

# tokenize で STRING / FSTRING_MIDDLE のトークン境界を正確に保持する (3.14 の列は文字単位 → バイトへ変換)
import io
import tokenize

FMID = getattr(tokenize, "FSTRING_MIDDLE", -2)
_lines = src.splitlines(keepends=True)
TOKS = []


def cpos(row, col):
    return offs[row - 1] + len(_lines[row - 1][:col].encode("utf-8"))


for tk in tokenize.generate_tokens(io.StringIO(src).readline):
    if tk.type == tokenize.STRING:
        TOKS.append((cpos(*tk.start), cpos(*tk.end), "S"))
    elif tk.type == FMID:
        TOKS.append((cpos(*tk.start), cpos(*tk.end), "M"))
    elif tk.type == getattr(tokenize, "FSTRING_START", -1):
        TOKS.append((cpos(*tk.start), cpos(*tk.end), "F"))
TOKS.sort()


def part_tokens(s, e):
    return [t for t in TOKS if t[0] < e and t[1] > s]


def enclosing_fn(n):
    f = par.get(id(n))
    while f is not None:
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True
        f = par.get(id(f))
    return False


def fstr_quote(node):
    """f-string 本体の引用文字 (slot 内の引用符を別種にして 3.8 の同一引用 nesting 禁止を回避)"""
    head = srcb[off(node.lineno, node.col_offset):off(node.lineno, node.col_offset) + 8].decode("ascii", "replace")
    m = re.search(r"[rfRfFfFfFfFf]*(['\"])", head)
    return m.group(1) if m else '"'


def pick_q(txt):
    """書き換え後 f-string リテラルの引用符: 本文と衝突せず改行を許す最小のやつ (無 then 空文字)"""
    for q in ("'", '"', "'''", '"""'):
        if q in txt:
            continue
        if "\n" in txt and len(q) == 1:
            continue
        if len(q) == 1 and txt.endswith(q):
            continue
        if len(q) == 3 and txt.endswith(q[0]):
            continue
        return q
    return ""


def fliteral(value, escaped=False):
    """値から単一の f-string リテラル源码を構築 (中括いは {{ }} エスケープ、日本語走りは t() スロット)。
    escaped=True は引用符衝突時の最終手段: " リテラル本体を \\-エスケープ (3.8 のリテラル部は可)"""
    segs = []
    for kind, seg in runs(value):
        if kind == "ja":
            segs.append(("slot", seg))
        else:
            s = seg.replace("{", "{{").replace("}", "}}")
            if escaped:
                s = s.replace("\\", "\\\\").replace('"', '\\"')
            segs.append(("txt", s))
    probe = "".join(s for k, s in segs if k == "txt")
    if escaped:
        q = '"""' if "\n" in probe else '"'
        if q in probe:
            return ""
        qk = "'"
    else:
        if "\\" in probe:
            return ""
        q = pick_q(probe)
        if not q:
            return fliteral(value, True)
        qk = '"' if q[0] == "'" else "'"
    def render(k, s):
        return ("{t(%s%s%s)}" % (qk, key(s), qk)) if k == "slot" else s
    groups, cur, n = [], [], 0
    # 500B/行制約: 隣接リテラル連結は値の連結と同じなので任意の文字位置で切れる (スロットは原子)
    for k, s in segs:
        rs = [render(k, s)] if k == "slot" else [render(k, s[i:i + 380]) for i in range(0, len(render(k, s)), 380)]
        for r in rs:
            if cur and n + len(r) > 380:
                groups.append(cur)
                cur, n = [], 0
            cur.append(r)
            n += len(r)
    if cur:
        groups.append(cur)
    return "\n            ".join("f" + q + "".join(g) + q for g in groups)


for node in ast.walk(tree):
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and JA_RUN.search(node.value):
        if skip_reason(node):
            continue
        p = par.get(id(node))
        in_fstr = isinstance(p, ast.JoinedStr)
        start, end = off(node.lineno, node.col_offset), off(node.end_lineno, node.end_col_offset)
        if in_fstr:
            raw = srcb[start:end].decode("utf-8")
            ov = part_tokens(start, end)
            strs = [t for t in ov if t[2] == "S"]
            mids = [t for t in ov if t[2] == "M"]
            if strs:
                # 畳み込み Constant: 通常リテラル連結域 (plain) か f-string 末尾文本との混在 (tail)。
                # 領域全体を閉じ引用まで置き換え、中括いエスケープ済みの新 f-string リテラルへ畳む
                out = fliteral(node.value)
                if not out:
                    warns.append((node.lineno, "folded literal: 変換不能 (手動対応)"))
                    continue
                tail = strs[0][0] != start or any(m[0] < strs[0][0] for m in mids)
                if tail:
                    # 元 f-string の閉じ引用 (Constant span の手前) を復元してから新リテラルを接続する
                    fs = [t for t in TOKS if t[2] == "F" and t[1] <= start]
                    q0 = srcb[fs[-1][0]:fs[-1][1]].decode()[-1] if fs else raw[:1]
                    out = q0 + out
                repl.append((start, strs[-1][1], out))
                if not enclosing_fn(node):
                    modlevel.append((node.lineno, node.value[:40]))
                continue
            qk = '"' if fstr_quote(p) == "'" else "'"
            if "{{" in raw or "}}" in raw:
                repl.append((start, end, "{t(%s%s%s)}" % (qk, key(node.value), qk)))
                continue
            parts = []
            for kind, seg in runs(raw):
                parts.append("{" + "t(%s%s%s)" % (qk, key(seg), qk) + "}" if kind == "ja" else seg)
            repl.append((start, end, "".join(parts)))
        else:
            parts = []
            for kind, seg in runs(node.value):
                parts.append("t(" + repr(key(seg)) + ")" if kind == "ja" else repr(seg))
            repl.append((start, end, " + ".join(parts)))
        if not enclosing_fn(node):
            modlevel.append((node.lineno, node.value[:40]))

b = bytearray(srcb)
for start, end, text in sorted(repl, key=lambda x: -x[0]):
    b[start:end] = text.encode("utf-8")
new_src = b.decode("utf-8")
try:
    ast.parse(new_src)  # 置換後の構文検証 (失敗時は生成物を保存して停止、.bak は無傷)
except SyntaxError as e:
    open(SRC + ".broken-src", "w", encoding="utf-8").write(new_src)
    print("SYNTAX FAIL at line %s: %s -> saved %s.broken-src" % (e.lineno, e.msg, SRC))
    sys.exit(1)

shutil.copy(SRC, SRC + ".bak")
open(SRC, "w", encoding="utf-8").write(new_src)

out = {k: v for v, k in sorted(keys.items(), key=lambda kv: kv[1])}  # key -> 原文 (順キー)
with open("minigit_ja.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
    fh.write("\n")
json.dump(out, open("keys.json", "w"), ensure_ascii=False, indent=1, sort_keys=True)

print("replaced:", len(repl), " keys:", len(keys), " bytes:", len(srcb), "->", len(new_src.encode()))
print("module-level replacements (要人手レビュー: import 時固定になっていないか):", len(modlevel))
for ln, s in modlevel[:20]:
    print("  L%d %s" % (ln, s))
for ln, w in warns:
    print("  WARN L%d %s" % (ln, w))

# 残存日本語定数 (docstring/css/re 等のスキップ残り) の一覧
tree2 = ast.parse(new_src)
par2 = {}
for n in ast.walk(tree2):
    for c in ast.iter_child_nodes(n):
        par2[id(c)] = n
par = par2
rest = []
for n in ast.walk(tree2):
    if isinstance(n, ast.Constant) and isinstance(n.value, str) and JA_RUN.search(n.value):
        r = skip_reason(n)
        if not r and not enclosing_fn(n):
            r = "MODULE?"
        rest.append((n.lineno, r or "LEFTOVER", n.value[:60].replace("\n", "\\n")))
rest.sort()
print("remaining JA constants: %d (leftover=%d)" % (len(rest), sum(1 for x in rest if x[1] == "LEFTOVER")))
for ln, r, s in rest:
    if r in ("LEFTOVER", "MODULE?"):
        print("  L%d %s %s" % (ln, r, s))
