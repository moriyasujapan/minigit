#!/usr/bin/env python3
r"""minigit - 閉域向け最小Gitホスティング
  - clone / push (Smart HTTP: git http-backend をCGIとして利用)
  - Issue / Pull Request / コメント / Merge (merge commit)
  - ファイル閲覧・コミット閲覧
依存: Python 3.8+ 標準ライブラリ, git 2.x (2.38+ なら merge-tree で高速マージ)

起動:
  MINIGIT_ROOT=/srv/repos python3 minigit.py 8080
ユーザー管理 (SQLite: ROOT/minigit.db の users テーブル, パスワードは PBKDF2-SHA256):
  - 初回起動時: MINIGIT_USERS="alice:pass1,bob:pass2" があれば取り込み (先頭が管理者)。
    なければ admin ユーザーをランダムパスワードで作成し、コンソールに表示。
  - 管理画面: /admin (追加・PW再設定・管理者付与・無効化・削除)、自分のPW変更: /account
  - 締め出された場合: python3 minigit.py passwd <ユーザー名> <新パスワード>
    (ユーザーが無ければ管理者として作成、無効なら有効化)
clone:
  git clone http://alice:pass1@host:8080/myrepo.git
git の場所: PATH -> %LOCALAPPDATA%\Programs\Git / %ProgramFiles%\Git を自動検索。
  明示する場合は環境変数で (変数展開可):
  set MINIGIT_GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe
"""
import os, re, sys, html, hmac, base64, socket, sqlite3, shutil, subprocess, tempfile, datetime
import hashlib, secrets, threading, difflib, shlex, json
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

def env_path(name, default):
    """環境変数のパス: 前後の空白・引用符 (バッチの set "..." 由来) を除去し %VAR% / ~ を展開"""
    v = (os.environ.get(name) or "").strip().strip("\"'").strip()
    return os.path.abspath(os.path.expanduser(os.path.expandvars(v or default)))


ROOT = env_path("MINIGIT_ROOT", "./repos")
DB = os.path.join(ROOT, "minigit.db")


def parse_users(s):
    """"alice:pw1,bob:pw2" -> dict。前後の空白・引用符 (cmd の set "..." 由来) は除去"""
    out = {}
    for item in s.strip().strip("\"'").split(","):
        item = item.strip().strip("\"'")
        if ":" in item:
            u, p = item.split(":", 1)
            if u.strip():
                out[u.strip()] = p.strip()
    return out


USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
PBKDF2_ITER = 120000


def find_git():
    """MINIGIT_GIT (%VAR% / $VAR / ~ 展開可) -> PATH -> Git for Windows の標準インストール先"""
    v = os.environ.get("MINIGIT_GIT")
    if v:
        return os.path.expanduser(os.path.expandvars(v.strip().strip('"')))
    w = shutil.which("git")
    if w:
        return w
    for base in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        d = os.environ.get(base)
        if not d:
            continue
        for sub in (("Programs", "Git", "cmd", "git.exe"), ("Git", "cmd", "git.exe")):
            p = os.path.join(d, *sub)
            if os.path.isfile(p):
                return p
    return "git"


def fix_git_launcher(p):
    """git-cmd.exe / git-bash.exe (プロンプトを開くランチャー) が指定されたら <Git>\\cmd\\git.exe に読み替え"""
    if os.path.basename(p).lower() in ("git-cmd.exe", "git-bash.exe", "git-cmd", "git-bash"):
        d = os.path.dirname(p)
        for cand in (os.path.join(d, "cmd", "git.exe"), os.path.join(d, "bin", "git.exe")):
            if os.path.isfile(cand):
                return cand
    return p


GIT = fix_git_launcher(find_git())
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{4,64}$")
E = html.escape


# ---------------------------------------------------------------- i18n (UI 文言は minigit_<lang>.py に分離: ja/zh/en)
LANGS = ("ja", "zh", "en")
DEFAULT_LANG = os.environ.get("MINIGIT_LANG", "en")  # 検出失敗時の既定。閉域網では MINIGIT_LANG=ja 推奨
STR = {}  # {lang: {key: text}}
_tl = threading.local()


def load_langs():
    """このファイルと同じ dir の minigit_<lang>.json ({"key": "文言"}) を読む。無い言語はスキップ (t() がフォールバック)"""
    here = os.path.dirname(os.path.abspath(__file__))
    for c in LANGS:
        p = os.path.join(here, f"minigit_{c}.json")
        try:
            with open(p, encoding="utf-8") as fh:
                STR[c] = json.load(fh)
        except (OSError, ValueError):
            continue


def lang_set(code):
    _tl.lang = code


def lang_get():
    return getattr(_tl, "lang", None) or DEFAULT_LANG


def t(key):
    """フォールバック: リクエスト言語 → en → ja (原語) → キー名そのもの"""
    for c in (lang_get(), "en", "ja"):
        s = STR.get(c, {}).get(key)
        if s is not None:
            return s
    return key


def lang_accept(header):
    for part in (header or "").split(","):
        code = part.split(";")[0].strip()[:2].lower()
        if code in LANGS:
            return code
    return ""


def langbar():
    """ヘッダの言語切替 (ja/中文/English は各言語表記のまま翻訳しない)。現 URL の query を保持し lang だけ差替"""
    path, query = getattr(_tl, "path", ("/", ""))
    items = []
    for code, label in (("ja", "日本語"), ("zh", "中文"), ("en", "English")):
        if code == lang_get():
            items.append("<b>" + label + "</b>")
        else:
            qs = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(query) if k != "lang"]
                                        + [("lang", code)])
            items.append('<a href="' + E(path) + "?" + qs + '">' + label + "</a>")
    return '<span class=lang>' + " · ".join(items) + "</span>"


load_langs()


# ---------------------------------------------------------------- git helpers
def repo_path(name):
    return os.path.join(ROOT, name + ".git")


GIT_TIMEOUT = int(os.environ.get("MINIGIT_TIMEOUT", "600"))


# サーバから実行する git では、裏で常駐/バックグラウンドプロセスを起動する機能を止める
# (Windows では子孫プロセスが出力ハンドルを握ったままになり、呼び出し側が戻らなくなるため)
GIT_SAFE_CONFIG = " ".join(f"'{kv}'" for kv in (
    "core.fsmonitor=false", "core.untrackedCache=false", "gc.auto=0", "gc.autoDetach=false",
    "maintenance.auto=false", "receive.autogc=false", "fetch.writeCommitGraph=false",
    "credential.helper=", "core.pager=cat", "core.quotepath=false"))


def run(args, check=True, input=None, env=None, cwd=None):
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
             GIT_PAGER="cat", PAGER="cat", GIT_CONFIG_PARAMETERS=GIT_SAFE_CONFIG)
    e.update(env or {})
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    t0 = datetime.datetime.now()
    # 出力はパイプではなく一時ファイルで受ける: 待つのは git プロセスの終了だけになる
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=fo, stderr=fe, env=e, cwd=cwd, creationflags=flags)
        try:
            proc.communicate(input=input, timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise RuntimeError(f"git {t('s191')} ({GIT_TIMEOUT}s): {' '.join(args[1:5])}")
        fo.seek(0)
        fe.seek(0)
        p = subprocess.CompletedProcess(args, proc.returncode, fo.read(), fe.read())
    sec = (datetime.datetime.now() - t0).total_seconds()
    if sec > 2 or os.environ.get("MINIGIT_DEBUG"):
        sys.stderr.write(f"[git {sec:.1f}s] {' '.join(str(a) for a in args[1:6])}\n")
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or "git failed")
    return p


def git(repo, *args, **kw):
    # リポジトリのパスは引数で渡さず cwd で指定 (日本語パス等の引数変換で別の場所を指すのを防ぐ)
    kw.setdefault("cwd", repo_path(repo))
    return run([GIT, "--git-dir=."] + list(args), **kw)


def gout(repo, *args):
    return git(repo, *args).stdout.decode(errors="replace")


def repos():
    if not os.path.isdir(ROOT):
        return []
    return sorted(d[:-4] for d in os.listdir(ROOT) if d.endswith(".git"))


def branches(repo):
    return [b for b in gout(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if b]


def head_branch(repo):
    p = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    return p.stdout.decode().strip() or "main"


def resolve(repo, ref):
    """ブランチ名 or SHA -> commit SHA (なければ None)"""
    if not ref or ref.startswith("-"):
        return None
    for cand in ("refs/heads/" + ref, ref if SHA_RE.match(ref) else None):
        if cand:
            p = git(repo, "rev-parse", "--verify", "--quiet", cand + "^{commit}", check=False)
            if p.returncode == 0:
                return p.stdout.decode().strip()
    return None


def ident(user):
    return {"GIT_AUTHOR_NAME": user, "GIT_AUTHOR_EMAIL": user + "@local",
            "GIT_COMMITTER_NAME": user, "GIT_COMMITTER_EMAIL": user + "@local"}


def merge_tree(repo, base, other, user):
    """(tree_sha, None) or (None, conflict_message)"""
    p = git(repo, "merge-tree", "--write-tree", "--no-messages", base, other, check=False)
    if p.returncode == 0:
        return p.stdout.decode().split()[0], None
    if p.returncode == 1:
        return None, p.stdout.decode(errors="replace")
    # 古い git (merge-tree --write-tree 非対応) -> 一時 worktree でマージ
    tmp = tempfile.mkdtemp(prefix="minigit-")
    try:
        git(repo, "worktree", "add", "--detach", tmp, base)
        m = run([GIT, "merge", "--no-commit", "--no-ff", other], cwd=tmp, env=ident(user), check=False)
        if m.returncode != 0:
            return None, (m.stdout + m.stderr).decode(errors="replace")
        return run([GIT, "write-tree"], cwd=tmp).stdout.decode().strip(), None
    finally:
        git(repo, "worktree", "remove", "--force", tmp, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)


def unrelated_msg():  # 遅延: 翻訳値をリクエスト言語で解決する (翻訳値に { } を入れないこと .format 使用)
    return (t('s001') + ' (' + t('s002') + ')\n' + t('s003') + ' git init ' + t('s004') + ' push ' + t('s005') +
            ':\n  A) ' + t('s006') + ' (' + t('s007') +
            '): git fetch origin && git checkout {src} && git merge origin/{dst} --allow-unrelated-histories\n'
            '  B) ' + t('s008') + ' target ' + t('s009') +
            ': git fetch origin && git rebase --root --onto origin/{dst} {src}\n     (B ' + t('s010') + ' push ' +
            t('s011') + ' -f ' + t('s012') + ': git push -f origin {src})')


def merge_base(repo, a, b):
    p = git(repo, "merge-base", a, b, check=False)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def mergeable(repo, src, dst, user="system"):
    s, d = resolve(repo, src), resolve(repo, dst)
    if not s or not d:
        return False, t('s021')
    if not merge_base(repo, s, d):
        return False, unrelated_msg().format(src=src, dst=dst)
    if git(repo, "merge-base", "--is-ancestor", s, d, check=False).returncode == 0:
        return False, t('s022') + ' (' + t('s023') + ')'
    tree, err = merge_tree(repo, d, s, user)
    if tree:
        return True, t('s024')
    files = []
    for line in err.splitlines():
        m = re.match(r"^\d{6} [0-9a-f]+ [123]\t(.+)$", line) or re.search(r"Merge conflict in (.+)$", line)
        if m and m.group(1) not in files:
            files.append(m.group(1))
    return False, (t('s080') + '\n' + t('s081') + ':\n' + "".join(f"  {f}\n" for f in files or ['(' + t('s245') + ')']) +
                   f"\n{t('s082')} ({t('s083')}):\n  git checkout {src}\n  git pull origin {dst}\n"
                   f"  ({t('s084')})\n  git push")


def do_merge(repo, src, dst, msg, user):
    s, d = resolve(repo, src), resolve(repo, dst)
    ok, why = mergeable(repo, src, dst, user)
    if not ok:
        raise RuntimeError(why)
    tree, _ = merge_tree(repo, d, s, user)
    new = git(repo, "commit-tree", tree, "-p", d, "-p", s, "-m", msg, env=ident(user)).stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/" + dst, new, d)  # d から変わっていたら失敗 (競合防止)
    return new


# ---------------------------------------------------------------- db
def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS item(
      repo TEXT, num INTEGER, kind TEXT, title TEXT, body TEXT, author TEXT,
      state TEXT DEFAULT 'open', src TEXT, dst TEXT, merge_sha TEXT, created TEXT,
      PRIMARY KEY(repo, num));
    CREATE TABLE IF NOT EXISTS comment(
      id INTEGER PRIMARY KEY, repo TEXT, num INTEGER, body TEXT, author TEXT, created TEXT,
      kind TEXT DEFAULT 'comment');
    CREATE TABLE IF NOT EXISTS protect(
      repo TEXT, name TEXT, PRIMARY KEY(repo, name));
    CREATE TABLE IF NOT EXISTS users(
      name TEXT PRIMARY KEY, pw TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
      active INTEGER NOT NULL DEFAULT 1, created TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT, expires REAL NOT NULL);
    """)
    try:  # 旧 DB からのマイグレーション (comment.kind)
        c.execute("ALTER TABLE comment ADD COLUMN kind TEXT DEFAULT 'comment'")
    except sqlite3.OperationalError:
        pass
    return c


# ---------------------------------------------------------------- users
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), PBKDF2_ITER).hex()
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${h}"


def check_pw(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode(), int(it)).hex()
    except Exception:
        return False
    return hmac.compare_digest(calc, h)


# 認証結果のキャッシュ (git は1操作で複数リクエストを送るため、毎回 PBKDF2 を計算しない)
_AUTH_CACHE = {}
_AUTH_LOCK = threading.Lock()


def users_changed():
    with _AUTH_LOCK:
        _AUTH_CACHE.clear()


def authenticate(name, pw):
    key = (name, hashlib.sha256(pw.encode("utf-8")).hexdigest())
    with _AUTH_LOCK:
        hit = _AUTH_CACHE.get(key)
    if hit:
        return hit
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE name=? AND active=1", (name,)).fetchone()
    if r and check_pw(pw, r["pw"]):
        info = {"name": r["name"], "admin": bool(r["admin"])}
        with _AUTH_LOCK:
            _AUTH_CACHE[key] = info
        return info
    return None


SESSION_DAYS = 30
SESSION_COOKIE = "minigit_session"


def _tok_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(name):
    token = secrets.token_urlsafe(32)
    import time
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        c.execute("INSERT INTO sessions(token,name,created,expires) VALUES(?,?,?,?)",
                  (_tok_hash(token), name, now(), time.time() + SESSION_DAYS * 86400))
    return token


def session_user(token):
    import time
    if not token:
        return None
    with db() as c:
        r = c.execute("SELECT u.name, u.admin FROM sessions s JOIN users u ON u.name=s.name "
                      "WHERE s.token=? AND s.expires>=? AND u.active=1", (_tok_hash(token), time.time())).fetchone()
    return {"name": r["name"], "admin": bool(r["admin"])} if r else None


def delete_session(token):
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (_tok_hash(token),))


def delete_user_sessions(name, keep_token=None):
    with db() as c:
        if keep_token:
            c.execute("DELETE FROM sessions WHERE name=? AND token<>?", (name, _tok_hash(keep_token)))
        else:
            c.execute("DELETE FROM sessions WHERE name=?", (name,))


def active_admins(c):
    return c.execute("SELECT COUNT(*) FROM users WHERE admin=1 AND active=1").fetchone()[0]


def set_password(name, pw, make_admin_if_new=True):
    """CLI 用: パスワード設定。ユーザーが無ければ作成 (管理者)、無効なら有効化"""
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
            c.execute("UPDATE users SET pw=?, active=1 WHERE name=?", (hash_pw(pw), name))
            msg = f"{name} {t('s085')} ({t('s086')})"
        else:
            c.execute("INSERT INTO users(name,pw,admin,active,created) VALUES(?,?,?,1,?)",
                      (name, hash_pw(pw), 1 if make_admin_if_new else 0, now()))
            msg = f"{t('s087')} {name} {t('s088')}"
    users_changed()
    return msg


def init_users():
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
        env = parse_users(os.environ.get("MINIGIT_USERS", ""))
        env = {u: p for u, p in env.items() if USER_RE.match(u) and p}
        if env:
            for i, (u, p) in enumerate(env.items()):
                c.execute("INSERT INTO users(name,pw,admin,active,created) VALUES(?,?,?,1,?)",
                          (u, hash_pw(p), 1 if i == 0 else 0, now()))
            return f"MINIGIT_USERS {t('s089')} {len(env)} {t('s090')} ({t('s087')}: {next(iter(env))})"
        pw = secrets.token_urlsafe(9)
        c.execute("INSERT INTO users(name,pw,admin,active,created) VALUES('admin',?,1,1,?)", (hash_pw(pw), now()))
        return f"{t('s025')}  {t('s026')}: admin  {t('s027')}: {pw}\n  ({t('s028')} /account {t('s029')})"


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- html
# Simple.css v2.3.7 | MIT License | Copyright (c) 2020 Simple.css (Kev Quirk) | https://simplecss.org
SIMPLE_CSS = r"""
:root{--sans-font:-apple-system,BlinkMacSystemFont,"Avenir Next",Avenir,"Nimbus Sans L",Roboto,"Noto Sans","Segoe UI",Arial,Helvetica,"Helvetica Neue",sans-serif;--mono-font:Consolas,Menlo,Monaco,"Andale Mono","Ubuntu Mono",monospace;--standard-border-radius:5px;--border-width:1px;--bg:#fff;--accent-bg:#f5f7ff;--text:#212121;--text-light:#585858;--border:#898ea4;--accent:#0d47a1;--accent-hover:#1266e2;--accent-text:var(--bg);--code:#d81b60;--preformatted:#444;--marked:#fd3;--disabled:#efefef}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#212121;--accent-bg:#2b2b2b;--text:#dcdcdc;--text-light:#ababab;--accent:#ffb300;--accent-hover:#ffe099;--accent-text:var(--bg);--code:#f06292;--preformatted:#ccc;--disabled:#111}
img,video{opacity:.8}
}
*,:before,:after{box-sizing:border-box}
textarea,select,input,progress{-webkit-appearance:none;-moz-appearance:none;appearance:none}
html{font-family:var(--sans-font);scroll-behavior:smooth}
body{color:var(--text);background-color:var(--bg);grid-template-columns:1fr min(45rem,90%) 1fr;margin:0;font-size:1.15rem;line-height:1.5;display:grid}
body>*{grid-column:2}
body>header{background-color:var(--accent-bg);border-bottom:var(--border-width)solid var(--border);text-align:center;grid-column:1/-1;padding:0 .5rem 2rem}
body>header>:only-child{margin-block-start:2rem}
body>header h1{max-width:1200px;margin:1rem auto}
body>header p{max-width:40rem;margin:1rem auto}
main{padding-top:1.5rem}
body>footer{color:var(--text-light);text-align:center;border-top:var(--border-width)solid var(--border);margin-top:4rem;padding:2rem 1rem 1.5rem;font-size:.9rem}
h1{font-size:3rem}
h2{margin-top:3rem;font-size:2.6rem}
h3{margin-top:3rem;font-size:2rem}
h4{font-size:1.44rem}
h5{font-size:1.15rem}
h6{font-size:.96rem}
p{margin:1.5rem 0}
p,h1,h2,h3,h4,h5,h6{overflow-wrap:break-word}
h1,h2,h3{line-height:1.1}
@media only screen and (width<=720px){h1{font-size:2.5rem}
h2{font-size:2.1rem}
h3{font-size:1.75rem}
h4{font-size:1.25rem}
}
a,a:visited{color:var(--accent)}
a:hover{text-decoration:none}
button,.button,a.button,input[type=submit],input[type=reset],input[type=button]{border:var(--border-width)solid var(--accent);background-color:var(--accent);color:var(--accent-text);padding:.5rem .9rem;line-height:normal;text-decoration:none}
.button[aria-disabled=true],input:disabled,textarea:disabled,select:disabled,button[disabled]{cursor:not-allowed;background-color:var(--disabled);border-color:var(--disabled);color:var(--text-light)}
input[type=range]{padding:0}
abbr[title]{cursor:help;text-decoration-line:underline;text-decoration-style:dotted}
button:enabled:hover,.button:not([aria-disabled=true]):hover,input[type=submit]:enabled:hover,input[type=reset]:enabled:hover,input[type=button]:enabled:hover{background-color:var(--accent-hover);border-color:var(--accent-hover);cursor:pointer}
.button:focus-visible,button:focus-visible:where(:enabled),input:enabled:focus-visible:where([type=submit],[type=reset],[type=button]){outline:2px solid var(--accent);outline-offset:1px}
header nav{padding:1rem 0 0;font-size:1rem;line-height:2}
header nav ul,header nav ol{flex-flow:wrap;place-content:space-around center;align-items:center;margin:0;padding:0;list-style-type:none;display:flex}
header nav ul li,header nav ol li{display:inline-block}
header nav a,header nav a:visited{border:var(--border-width)solid var(--border);border-radius:var(--standard-border-radius);color:var(--text);margin:0 .5rem 1rem;padding:.1rem 1rem;text-decoration:none;display:inline-block}
header nav a:hover,header nav a.current,header nav a[aria-current=page],header nav a[aria-current=true]{border-color:var(--accent);color:var(--accent);cursor:pointer}
@media only screen and (width<=720px){header nav a{border:none;padding:0;line-height:1;text-decoration:underline}
}
aside,details,pre,progress{background-color:var(--accent-bg);border:var(--border-width)solid var(--border);border-radius:var(--standard-border-radius);margin-bottom:1rem}
aside{float:right;width:30%;margin-inline-start:15px;padding:0 15px;font-size:1rem}
[dir=rtl] aside{float:left}
@media only screen and (width<=720px){aside{float:none;width:100%;margin-inline-start:0}
}
article,fieldset,dialog{border:var(--border-width)solid var(--border);border-radius:var(--standard-border-radius);margin-bottom:1rem;padding:1rem}
article h2:first-child,section h2:first-child,article h3:first-child,section h3:first-child{margin-top:1rem}
section{border-top:var(--border-width)solid var(--border);border-bottom:var(--border-width)solid var(--border);margin:3rem 0;padding:2rem 1rem}
section+section,section:first-child{border-top:0;padding-top:0}
section+section{margin-top:0}
section:last-child{border-bottom:0;padding-bottom:0}
details{padding:.7rem 1rem}
summary{cursor:pointer;word-break:break-all;margin:-.7rem -1rem;padding:.7rem 1rem;font-weight:700}
details[open]>summary+*{margin-top:0}
details[open]>summary{margin-bottom:.5rem}
details[open]>:last-child{margin-bottom:0}
table{border-collapse:collapse;margin:1.5rem 0}
figure>table{width:max-content;margin:0}
td,th{border:var(--border-width)solid var(--border);text-align:start;padding:.5rem}
th{background-color:var(--accent-bg);font-weight:700}
tr:nth-child(2n){background-color:var(--accent-bg)}
table caption{margin-bottom:.5rem;font-weight:700}
textarea,select,input,button,.button{font-size:inherit;border-radius:var(--standard-border-radius);box-shadow:none;max-width:100%;margin-bottom:.5rem;padding:.5em;font-family:inherit;display:inline-block}
textarea,select,input{color:var(--text);background-color:var(--bg);border:var(--border-width)solid var(--border)}
label{display:block}
textarea:not([cols]){width:100%}
select:not([multiple]){background-image:linear-gradient(45deg,transparent 49%,var(--text)51%),linear-gradient(135deg,var(--text)51%,transparent 49%);background-position:calc(100% - 15px),calc(100% - 10px);background-repeat:no-repeat;background-size:5px 5px,5px 5px;padding-inline-end:25px}
[dir=rtl] select:not([multiple]){background-position:10px,15px}
input[type=checkbox],input[type=radio]{vertical-align:middle;width:min-content;position:relative}
input[type=checkbox]+label,input[type=radio]+label{display:inline-block}
input[type=radio]{border-radius:100%}
input[type=checkbox]:checked,input[type=radio]:checked{background-color:var(--accent)}
input[type=checkbox]:checked:after{content:" ";border-right:solid var(--bg).08em;border-bottom:solid var(--bg).08em;background-color:#0000;border-radius:0;width:.2em;height:.4em;font-size:1.8em;position:absolute;top:.04em;left:.18em;transform:rotate(45deg)}
input[type=radio]:checked:after{content:" ";background-color:var(--bg);border-radius:100%;width:.3em;height:.3em;font-size:1.8em;position:absolute;top:.125em;left:.125em}
@media only screen and (width<=720px){textarea,select,input{width:100%}
}
input[type=color]{height:2.5rem;padding:.2rem}
input[type=file]{border:0}
hr{height:var(--border-width);background:var(--border);border:none;margin:1rem auto}
mark{border-radius:var(--standard-border-radius);background-color:var(--marked);color:#000;padding:2px 5px}
mark a{color:#0d47a1}
img,video{border-radius:var(--standard-border-radius);max-width:100%;height:auto}
figure{margin:0;display:block;overflow-x:auto}
figure>img,figure>picture>img{margin-inline:auto;display:block}
figcaption{text-align:center;color:var(--text-light);margin-block:1rem;font-size:.9rem;position:sticky;left:0}
blockquote{border-inline-start:.35rem solid var(--accent);color:var(--text-light);margin-block:2rem;margin-inline:2rem 0;padding:.4rem .8rem;font-style:italic}
cite{color:var(--text-light);font-size:.9rem;font-style:normal}
dt{color:var(--text-light)}
code,pre,pre span,kbd,samp{font-family:var(--mono-font);color:var(--code)}
kbd{color:var(--preformatted);border:var(--border-width)solid var(--preformatted);border-bottom:3px solid var(--preformatted);border-radius:var(--standard-border-radius);padding:.1rem .4rem}
pre{max-width:100%;color:var(--preformatted);padding:1rem 1.4rem;overflow:auto}
pre code{color:var(--preformatted);background:0 0;margin:0;padding:0}
progress{width:100%}
progress:indeterminate{background-color:var(--accent-bg)}
progress::-webkit-progress-bar{border-radius:var(--standard-border-radius);background-color:var(--accent-bg)}
progress::-webkit-progress-value{border-radius:var(--standard-border-radius);background-color:var(--accent)}
progress::-moz-progress-bar{border-radius:var(--standard-border-radius);background-color:var(--accent);transition-property:width;transition-duration:.3s}
progress:indeterminate::-moz-progress-bar{background-color:var(--accent-bg)}
dialog{background-color:var(--bg);max-width:40rem;margin:auto}
dialog::backdrop{background-color:var(--bg);opacity:.8}
@media only screen and (width<=720px){dialog{max-width:calc(100vw - 2rem)}
}
sup,sub{vertical-align:baseline;position:relative}
sup{top:-.4em}
sub{top:.3em}
.notice{background:var(--accent-bg);border:var(--border-width)solid var(--border);border-radius:var(--standard-border-radius);margin:2rem 0;padding:1.5rem}
@media print{@page{margin:1cm}
body{display:block}
body>header{background-color:unset}
body>header nav,body>footer{display:none}
article{border:none;padding:0}
a[href^=http]:after{content:" <" attr(href)">"}
abbr[title]:after{content:" (" attr(title)")"}
a{text-decoration:none}
p{widows:3;orphans:3}
hr{border-top:var(--border-width)solid var(--border)}
mark{border:var(--border-width)solid var(--border)}
pre,table,figure,img,svg{break-inside:avoid}
pre code{white-space:pre-wrap}
}
"""
APP_CSS = r""":root{--border:#c9cfda;--ok:#1a7f37;--danger:#cf222e;--ng:#d4a72c;--a:#1a7f37;--r:#cf222e;--h-kw:#cf222e;--h-st:#0a3069;--h-cm:#6e7781;--h-nm:#0550ae}
.hw{color:var(--h-kw)}.hs{color:var(--h-st)}.hc{color:var(--h-cm);font-style:italic}.hn{color:var(--h-nm)}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
body{grid-template-columns:1fr min(72rem,94%) 1fr;font-size:.9rem;line-height:1.55}
main{padding-bottom:3.5rem}
textarea,select,input{padding:.32em .6em;margin-bottom:.45rem}
button,.button,input[type=submit],input[type=button]{padding:.34em .8em;margin-bottom:.45rem;font-size:.9rem}
body>header{padding:.4rem 1rem;text-align:left}
body>header nav{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem;margin:0;padding:0;font-size:.85rem;line-height:1.6}
body>header nav a{margin:0;padding:.1rem .6rem}
body>header nav .brand{font-weight:700;border:0;padding-left:0;font-size:.95rem}
body>header nav .repo{font-weight:700;margin:0 .3rem 0 .6rem}
body>header nav .me{margin-left:auto;display:flex;gap:.25rem;align-items:center;font-size:.82rem}
main{padding-top:.7rem}
h2{margin:.3rem 0 .7rem;font-size:1.22rem}h3{font-size:1.02rem;margin:1.2rem 0 .4rem}
h2 small{color:var(--text-light);font-weight:400}
table{width:100%;font-size:.87rem;margin:.8rem 0}td,th{padding:.32rem .55rem;vertical-align:top}
pre{font-size:.82rem;line-height:1.45;padding:.8rem 1rem;max-height:none}
pre .add{background:rgba(46,160,67,.2);display:inline-block;min-width:100%}
pre .del{background:rgba(248,81,73,.2);display:inline-block;min-width:100%}
pre .hunk{color:var(--accent)}
.st{display:inline-block;padding:.05rem .6rem;border-radius:1rem;color:#fff;font-size:.8rem;font-weight:600}
.open{background:var(--ok)}.closed{background:var(--danger)}.merged{background:#8250df}
.approve{background:var(--ok)}.change{background:var(--danger)}
.md{max-width:62rem}
.md .mdh{margin:1.1rem 0 .4rem}
.md img{max-width:100%}
.md blockquote{border-left:3px solid var(--border);margin:.5rem 0;padding:.2rem .9rem;color:var(--text-light)}
.md p,.md ul,.md ol{margin:.45rem 0}
.md code{background:var(--preformatted);color:var(--code);padding:.08rem .3rem;border-radius:.25rem}
.md pre code{padding:0;background:none}
.rdh{margin:1.4rem 0 0;font-size:1rem}
.mdbox{border:1px solid var(--border);border-radius:.5rem;padding:.2rem 1.3rem 1.1rem;margin-top:.5rem;background:var(--bg)}
.clonebar{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem}
.clonebar pre{flex:1;margin:0;padding:.5rem .8rem;border-radius:.4rem}
.scrollbox{max-height:24rem;overflow-y:auto;border:1px solid var(--border);border-radius:.5rem}
.scrollbox table{margin:0}
.box{border:1px solid var(--border);border-radius:var(--standard-border-radius);padding:.65rem .9rem;margin:.8rem 0}
.box>b:first-child,.box>form>b:first-child{display:block;margin-bottom:.4rem}
.box pre{margin:.5rem 0}
.msg{background:var(--accent-bg);border-left:4px solid var(--accent);padding:.5rem .9rem;margin:0 0 .8rem;border-radius:var(--standard-border-radius)}
.box input[type=text],.box input[type=password],.box select,.box textarea{width:100%;box-sizing:border-box;margin:.25rem 0}
.box textarea{min-height:6rem}
label{margin:.4rem 0 0}
td form{display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;margin:0}
td input{width:9em;margin:0;padding:.3rem .5rem}
td button,form.inline button,.sm{padding:.25rem .65rem;font-size:.85rem;margin:0}
form.inline{display:inline;margin:0}
button.danger{background:var(--danger);border-color:var(--danger)}
.warn{color:#cf222e;font-weight:600}.muted{color:var(--text-light)}
.tabs{display:flex;gap:.8rem;margin:0 0 .6rem}.tabs b{border-bottom:2px solid var(--accent)}
code{font-size:.85em}
body>header>*:only-child{margin-block-start:0}
pre,pre code{color:var(--text)}
button.ok{background:var(--ok);border-color:var(--ok)}
button.sub{background:transparent;color:var(--text);border:1px solid var(--border)}
button.sub:hover{background:var(--accent-bg)}
.box .muted{margin:0}
.login{max-width:26rem;margin:2.2rem auto}.login label{display:block;margin:.5rem 0}
.login input{width:100%;box-sizing:border-box}.msg.err{border-left-color:var(--danger)}
.me form.inline button{margin-left:.2rem}
a.btn{display:inline-block;padding:.5rem .9rem;border-radius:var(--standard-border-radius);background:var(--accent);color:var(--bg);text-decoration:none;font-size:1rem;line-height:1.2}
a.btn.ok{background:var(--ok)}a.btn.sub{background:transparent;color:var(--text);border:1px solid var(--border)}
a.btn:hover{filter:brightness(1.1)}
body.wide{grid-template-columns:1fr min(112rem,98%) 1fr;font-size:.87rem;line-height:1.5}
body.wide h2{font-size:1.18rem}body.wide h3{font-size:1rem}
body.wide button,body.wide a.btn{font-size:.82rem;padding:.3rem .75rem}
body.wide textarea,body.wide input,body.wide select{font-size:.85rem}
body.wide .pr-meta{font-size:.85rem}body.wide .mergebar{padding:.5rem .9rem}
.a{color:var(--a);font-weight:600}.r{color:var(--r);font-weight:600}
.pr-head h2{margin-bottom:.3rem}.pr-meta{font-size:.95rem;line-height:1.8}
.mergebar{display:flex;flex-wrap:wrap;gap:.8rem;align-items:center;justify-content:space-between;border:1px solid var(--border);border-left:5px solid var(--border);border-radius:var(--standard-border-radius);padding:.5rem .9rem;margin:.8rem 0}
.mergebar.ok{border-left-color:var(--ok)}.mergebar.ng{border-left-color:var(--ng)}.mergebar.merged{border-left-color:#8250df}.mergebar.closed{border-left-color:var(--danger)}
.mergebar>div{flex:1 1 20rem}.mergebar button,.mergebar form{margin:0}
.howto{border:0;padding:0;margin:.3rem 0 0;background:none}.howto summary{font-size:.85rem;color:var(--accent);cursor:pointer}
.howto pre{margin:.3rem 0 0}
.pr-layout{display:grid;grid-template-columns:17rem minmax(0,1fr);gap:1rem;align-items:start}
.pr-side{position:sticky;top:.5rem;max-height:calc(100vh - 1rem);overflow:auto;border:1px solid var(--border);border-radius:var(--standard-border-radius);font-size:.85rem}
.side-h{font-weight:700;padding:.45rem .7rem;background:var(--accent-bg);border-bottom:1px solid var(--border);position:sticky;top:0}
.flist,.clist{list-style:none;margin:0;padding:.2rem 0}
.flist a{display:flex;gap:.4rem;align-items:center;padding:.25rem .6rem;text-decoration:none;color:var(--text)}
.flist a:hover,.clist li:hover{background:var(--accent-bg)}
.fn{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fn small{display:block;color:var(--text-light);font-size:.75rem;overflow:hidden;text-overflow:ellipsis}
.fc{white-space:nowrap;font-size:.78rem}
.clist li{padding:.3rem .7rem;border-top:1px solid var(--border)}.clist li:first-child{border-top:0}
.clist a{text-decoration:none;color:var(--text);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.clist small{color:var(--text-light)}
.fs{display:inline-block;min-width:1.3rem;text-align:center;border-radius:4px;font-size:.72rem;font-weight:700;color:#fff;padding:0 .2rem}
.sA{background:#1a7f37}.sM{background:#9a6700}.sD{background:#cf222e}.sR{background:#0969da}
.viewsw{display:flex;gap:.3rem;align-items:center;font-size:.85rem;margin-bottom:.5rem}
.viewsw a{padding:.15rem .7rem;border:1px solid var(--border);border-radius:var(--standard-border-radius);text-decoration:none}
.viewsw a.on{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.legend{display:grid;grid-template-columns:1fr 1fr;font-size:.8rem;color:var(--text-light);margin-bottom:.3rem}.legend span:last-child{text-align:right}
details.file{border:1px solid var(--border);border-radius:var(--standard-border-radius);padding:0;margin:0 0 1rem;background:var(--bg)}
details.file>summary{display:flex;gap:.5rem;align-items:center;position:sticky;top:0;z-index:2;background:var(--accent-bg);padding:.45rem .8rem;margin:0;border-bottom:1px solid var(--border);font-size:.85rem;font-weight:400;border-radius:var(--standard-border-radius) var(--standard-border-radius) 0 0}
details.file:not([open])>summary{border-bottom:0}
details.file>summary .path{flex:1;font-family:var(--mono-font);word-break:break-all;color:var(--text)}
table.d{width:100%;margin:0;border-collapse:collapse;table-layout:fixed;font-family:var(--mono-font);font-size:.78rem;line-height:1.5;border:0;display:table}
table.d tr,table.d tr:nth-child(even){background:none}
table.d td{border:0;padding:0 .5rem;vertical-align:top;white-space:pre-wrap;word-break:break-all;tab-size:4}
table.d col.nc{width:3.6em}
table.d td.n{text-align:right;color:var(--text-light);user-select:none;padding:0 .4rem;font-size:.72rem;line-height:1.65}
table.split td.n:nth-child(3){border-left:1px solid var(--border)}
table.d td.del{background:rgba(248,81,73,.13)}table.d td.add{background:rgba(46,160,67,.14)}
table.d td.n.del{background:rgba(248,81,73,.2)}table.d td.n.add{background:rgba(46,160,67,.22)}
table.d td.e{background:repeating-linear-gradient(135deg,transparent 0 6px,var(--accent-bg) 6px 12px)}
table.d del{background:rgba(248,81,73,.45);text-decoration:none;border-radius:2px}
table.d ins{background:rgba(46,160,67,.45);text-decoration:none;border-radius:2px}
table.d tr.h td{background:rgba(9,105,218,.08);color:var(--text-light);padding:.2rem .6rem;font-size:.75rem}
.pad{padding:.6rem 1rem;margin:0}
h3.conv{margin-top:2rem}pre.txt{white-space:pre-wrap;font-family:var(--sans-font);font-size:.95rem;background:none;border:0;padding:.3rem 0;margin:0}
.rfile{border:1px solid var(--border);border-radius:var(--standard-border-radius);margin:1rem 0;padding:0 0 .6rem}
.rhead{display:flex;gap:1rem;justify-content:space-between;background:var(--accent-bg);padding:.45rem .8rem;border-bottom:1px solid var(--border);font-size:.9rem}
.rhead .path{font-family:var(--mono-font);font-weight:700}
.rcols{display:grid;grid-template-columns:1fr 1fr;gap:0;border-bottom:1px solid var(--border)}
.rpane{min-width:0}.rpane+.rpane{border-left:1px solid var(--border)}
.rpl{font-size:.78rem;color:var(--text-light);padding:.3rem .8rem;border-bottom:1px dashed var(--border)}
.rpane pre{margin:0;border:0;border-radius:0;max-height:22rem;overflow:auto;font-size:.78rem;background:none}
.ropts{display:flex;flex-wrap:wrap;gap:1.2rem;padding:.6rem .8rem 0}.ropts label{margin:0;display:flex;gap:.3rem;align-items:center;font-size:.9rem}
.rfile textarea.code{display:block;width:calc(100% - 1.6rem);margin:.5rem .8rem 0;font-family:var(--mono-font);font-size:.8rem;line-height:1.5;tab-size:4;white-space:pre}
.hint{font-size:.78rem;color:var(--text-light);margin:.3rem .8rem 0}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#0d1117;--accent-bg:#161b22;--text:#e6edf3;--text-light:#9198a1;--border:#3d444d;--accent:#2f81f7;--accent-hover:#58a6ff;--accent-text:var(--bg);--code:#f778ba;--preformatted:#262c36;--disabled:#21262d;--ok:#238636;--danger:#da3633;--ng:#d29922;--a:#3fb950;--r:#f85149;--h-kw:#ff7b72;--h-st:#a5d6ff;--h-cm:#8b949e;--h-nm:#79c0ff}}
[data-theme=dark]{color-scheme:dark;--bg:#0d1117;--accent-bg:#161b22;--text:#e6edf3;--text-light:#9198a1;--border:#3d444d;--accent:#2f81f7;--accent-hover:#58a6ff;--accent-text:var(--bg);--code:#f778ba;--preformatted:#262c36;--disabled:#21262d;--ok:#238636;--danger:#da3633;--ng:#d29922;--a:#3fb950;--r:#f85149;--h-kw:#ff7b72;--h-st:#a5d6ff;--h-cm:#8b949e;--h-nm:#79c0ff}
[data-theme=light]{color-scheme:light;--bg:#fff;--accent-bg:#f5f7ff;--text:#212121;--text-light:#585858;--border:#c9cfda;--accent:#0d47a1;--accent-hover:#1266e2;--code:#d81b60;--preformatted:#eee;--disabled:#efefef;--ok:#1a7f37;--danger:#cf222e;--ng:#d4a72c;--a:#1a7f37;--r:#cf222e;--h-kw:#cf222e;--h-st:#0a3069;--h-cm:#6e7781;--h-nm:#0550ae}
@media (max-width:900px){.pr-layout{grid-template-columns:1fr}.pr-side{position:static;max-height:18rem}.rcols{grid-template-columns:1fr}.rpane+.rpane{border-left:0;border-top:1px solid var(--border)}}"""
CSS = SIMPLE_CSS + "\n" + APP_CSS
CSS_BYTES = CSS.encode("utf-8")
CSS_VER = hashlib.sha256(CSS_BYTES).hexdigest()[:8]  # 変更時だけ再読込させる (1日キャッシュでも壊れない)


def page(title, body, repo=None, wide=False):
    nav = '<a class=brand href="/">minigit</a>'
    if repo:
        r = urllib.parse.quote(repo)
        nav += (f'<span class=repo>{E(repo)}</span><a href="/r/{r}">code</a><a href="/r/{r}/issues">issues</a>'
                f'<a href="/r/{r}/pulls">pulls</a><a href="/r/{r}/branches">branches</a>')
    nav += ('<a href=# onclick="return mgT()" title="' + t('s030') + '/' + t('s031') + '">&#9680;</a>' + langbar()
            + '<span class=me><!--ME--></span>')
    return ("<!doctype html><html lang=" + lang_get() + "><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} - minigit</title><link rel=stylesheet href='/_/style.css?v={CSS_VER}"f"'><script>var h=document.documentElement;if(localStorage.mg_t)h.dataset.theme=localStorage.mg_t;function mgT(){{var c=h.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');h.dataset.theme=localStorage.mg_t=(c=='dark'?'light':'dark');return false}}function mgC(b){{var t=b.dataset.u?fetch(b.dataset.u).then(function(r){{return r.text()}}):Promise.reso"
            f"lve((document.getElementById(b.dataset.t||'mgpre')||{{}}).innerText||'');t.then(function(s){{var a=document.createElement('textarea');a.value=s;a.style.cssText='position:fixed;top:-3rem;opacity:0';document.body.appendChild(a);a.select();a.focus();try{{document.execCommand('copy')}}catch(e){{}}document.body.removeChild(a);var o=b.textContent;b.textContent='{t('s032')}"
            f"';setTimeout(function(){{b.textContent=o}},1200)}})}}</script>"
            f"<body{' class=wide' if wide else ''}><header><nav>{nav}</nav></header><main>{body}</main></body></html>")


# ---------------------------------------------------------------- 構文ハイライト (自作トークナイザ。JS 不要)
HL_EXT = {"py": "py", "java": "java", "js": "js", "mjs": "js", "cjs": "js", "ts": "js", "jsx": "js", "tsx": "js",
          "c": "c", "h": "c", "cc": "c", "cpp": "c", "hpp": "c", "cxx": "c", "sh": "sh", "bash": "sh", "ksh": "sh",
          "zsh": "sh", "sql": "sql", "html": "html", "htm": "html", "xml": "html", "svg": "html", "vue": "html",
          "css": "css", "scss": "css", "ini": "ini", "properties": "ini", "conf": "ini", "cfg": "ini", "toml": "ini"}
_LANGS = {  # 言語: (キーワード, 行コメント, ブロックコメント, 3連クォート, 大文字小無視)
    "py": ("def class if elif else for while return import from as with try except finally raise pass break continue "
           "lambda global nonlocal del assert yield async await None True False and or not in is self print range len "
           "str int float list dict set tuple type super", ("#",), None, ('"""', "'''"), 0),
    "java": ("public private protected static final class interface enum extends implements new return if else for "
             "while do switch case default break continue try catch finally throw throws package import this super "
             "void int long short byte float double boolean char true false null instanceof abstract synchronized "
             "volatile transient native String var record sealed yield", ("//",), ("/*", "*/"), (), 0),
    "js": ("function var let const return if else for while do switch case default break continue new class extends "
           "super this typeof instanceof in of delete void yield async await try catch finally throw import export "
           "from as null undefined true false static get set debugger", ("//",), ("/*", "*/"), ("`",), 0),
    "c": ("auto break case char const continue default do double else enum extern float for goto if inline int long "
          "register return short signed sizeof static struct switch typedef union unsigned void volatile while",
          ("//",), ("/*", "*/"), (), 0),
    "sh": ("if then else elif fi case esac for while until do done in function return exit local export unset set "
           "shift read echo printf test trap eval source alias break continue select time", ("#",), None, (), 0),
    "sql": ("select insert update delete from where set values create table drop alter rename add column primary key "
            "foreign references unique index view and or not null as join inner left right full outer cross natural "
            "on using group by order having limit offset union all distinct case when then else end exists between "
            "like is asc desc count sum avg min max coalesce cast commit rollback begin transaction grant revoke int "
            "varchar text date timestamp", ("--",), ("/*", "*/"), (), 1),
    "css": ("", (), ("/*", "*/"), (), 1),
    "ini": ("", ("#", ";"), None, (), 0),
}
_HLRX, _HLK = {}, {}


def _mk_rx(lang):
    r = _HLRX.get(lang)
    if r is None:
        kw, lc, bc, tri, ci = _LANGS[lang]
        _HLK[lang] = frozenset((x.lower() if ci else x) for x in kw.split())
        p = [re.escape(q) + r".*?" + re.escape(q) + r"|" + re.escape(q) + r".*$" for q in tri]
        if bc:
            p.append(re.escape(bc[0]) + r".*?" + re.escape(bc[1]) + r"|" + re.escape(bc[0]) + r".*$")
        p += [re.escape(x) + r".*$" for x in lc]
        p.append(r'"(?:[^"\\\n]|\\.)*"?|\'(?:[^\'\\\n]|\\.)*\'?')
        p.append(r"\b[0-9][0-9a-zA-Z._]*")
        p.append(r"[@$]?[A-Za-z_][\w$]*")
        r = _HLRX[lang] = re.compile("|".join("(?:" + x + ")" for x in p))
    return r


def _sp(c, s):
    return f'<span class={c}>{s}</span>' if c else s


def _hl_cls(lang, s):
    kw, lc, bc, tri, ci = _LANGS[lang]
    if any(s.startswith(x) for x in lc):
        return "hc", None
    if bc and s.startswith(bc[0]):
        return ("hc", (bc[1], "hc")) if not s.endswith(bc[1]) else ("hc", None)
    for q in tri:
        if s.startswith(q):
            return ("hs", None) if s.count(q) >= 2 and len(s) > 2 * len(q) else ("hs", (q, "hs"))
    if s[0] in "\"'":
        return "hs", None
    if s.startswith("@"):
        return "hw", None
    if s[0].isdigit():
        return "hn", None
    return ("hw", None) if (s.lower() if ci else s) in _HLK[lang] else (None, None)


def _hl_code(line, lang, st):
    out, pos = [], 0
    while pos < len(line):
        if st:
            close, cls = st
            j = line.find(close, pos)
            if j < 0:
                out.append(_sp(cls, E(line[pos:])))
                return "".join(out), st
            out.append(_sp(cls, E(line[pos:j + len(close)])))
            pos, st = j + len(close), None
            continue
        m = _mk_rx(lang).search(line, pos)
        if not m:
            out.append(E(line[pos:]))
            break
        out.append(E(line[pos:m.start()]))
        cls, st = _hl_cls(lang, m.group(0))
        out.append(_sp(cls, E(m.group(0))))
        pos = m.end()
    return "".join(out), st


def _hl_html_line(line):
    out, pos = [], 0
    for m in re.finditer(r"(<!--.*$)|</?[A-Za-z][^>]*>?|[^<]+|<", line):
        out.append(E(line[pos:m.start()]))
        s = m.group(0)
        if s.startswith("<!--"):
            out.append(_sp("hc", E(s)))
        elif s.startswith("</") or (s.startswith("<") and s[1:2].isalpha()):
            buf, i = [], 0
            for t in re.finditer(r"[\"'][^\"']*[\"']|[\"'][^\"']*$|</?[\w:.-]+|[\w:.-]+(?==)", s):
                buf.append(E(s[i:t.start()]))
                g = t.group(0)
                out_cls = "hs" if g[0] in "\"'" else ("hw" if g.startswith("<") else "hn")
                buf.append(_sp(out_cls, E(g)))
                i = t.end()
            buf.append(E(s[i:]))
            out.append("".join(buf))
        else:
            out.append(E(s))
        pos = m.end()
    out.append(E(line[pos:]))
    return "".join(out)


def hl_lang(path):
    base = (path or "").rpartition(".")[2].lower()
    return HL_EXT.get(base, "") if "." in (path or "") else ""


def hl(text, lang):
    if not lang or (lang != "html" and lang not in _LANGS) or len(text) > 300000 or "\0" in text[:8000]:
        # html は _hl_html_line 側 (キーワード表外)。未知言語は丸エスケープ
        return E(text)
    if lang == "html":
        return "\n".join(_hl_html_line(x) for x in text.split("\n"))
    st, out = None, []
    for x in text.split("\n"):
        r, st = _hl_code(x, lang, st)
        out.append(r)
    return "\n".join(out)


MD_SAFE = re.compile(r"^(https?:|mailto:|ftp:|#|\.{0,2}/)", re.I)
RAW_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
             "webp": "image/webp", "ico": "image/x-icon", "svg": "image/svg+xml",
             "css": "text/css", "js": "text/javascript", "txt": "text/plain", "md": "text/plain"}
# フェンスの ```名前 はファイル拡張子と別表記があるためここで _LANGS のキーに寄せる
MD_FENCE = {"python": "py", "golang": "go", "sh": "sh", "shell": "sh", "bash": "sh", "zsh": "sh",
            "javascript": "js", "typescript": "js", "yml": "ini", "ps1": "ini", "bat": "ini", "console": "sh"}


def md_fence_lang(name):
    return HL_EXT.get(name.lower(), MD_FENCE.get(name.lower(), name.lower()))


def _md_url(u, base, repo, ref):
    """相対リンク/画像を minigit 内の URL に解決。危険スキームは # に"""
    if MD_SAFE.match(u):
        return u
    head = u.split("/")[0]
    if ":" in head:
        return "#"
    segs = [x for x in base.split("/") if x and x != "."]
    for x in u.split("/"):
        if x in ("", "."):
            continue
        if x == "..":
            segs = segs[:-1]
        else:
            segs.append(x)
    p = "/".join(segs)
    if re.search(r"\.(png|jpe?g|gif|svg|webp|ico)$", p, re.I) and repo:
        return f"/r/{repo}/tree?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(p)}&raw=1"
    return f"/r/{repo}/tree?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(p)}" if repo else p


def md_inline(s, base="", repo=None, ref=""):
    out = []
    for i, p in enumerate(re.split(r"(`+)([^`]*)\1", s)):
        if i % 3 == 2:
            out.append("<code>" + E(p) + "</code>")
        elif i % 3 == 1:
            continue
        else:
            t = E(p)
            t = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)",
                       lambda m: f'<img alt="{m.group(1)}" src="{_md_url(m.group(2), base, repo, ref)}">', t)
            t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                       lambda m: f'<a href="{_md_url(m.group(2), base, repo, ref)}">{m.group(1)}</a>', t)
            t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
            t = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<i>\1</i>", t)
            t = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", t)
            out.append(t)
    return "".join(out)


def _md_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_list(lines, i, out, base="", repo=None, ref=""):
    """i 行目からのリスト塊を消費して out に HTML を足し、次の行番号を返す"""
    items = []
    while i < len(lines):
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
        if m:
            ind, ordered = len(m.group(1).replace("\t", "  ")), m.group(2)[0].isdigit()
            if items and ind <= items[0][0] and ordered != items[0][1]:
                break  # "- x" 直後の "1. y" は別リスト (GitHub と同じ扱い)
            items.append([ind, ordered, m.group(3)])
        elif items and not lines[i].strip():
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[j]):
                i = j - 1  # 緩いリスト: 空行を飛ばして継続 (マーカー自体は上の規則で別リスト判定)
            elif j < len(lines) and lines[j][:1] in (" ", "\t"):
                items[-1][2] += "\n" + lines[j].strip()
                i = j
            else:
                break
        elif items and lines[i][:1] in (" ", "\t"):
            items[-1][2] += "\n" + lines[i].strip()
        else:
            break
        i += 1

    def build(k, indent):
        tag = "ol" if items[k][1] else "ul"
        h = [f"<{tag}>"]
        while k < len(items) and items[k][0] >= indent:
            if items[k][0] > indent:
                sub, k2 = build(k, items[k][0])
                h[-1] = h[-1].replace("</li>", sub + "</li>")
                k = k2
                continue
            body, k2 = (build(k + 1, items[k + 1][0]) if k + 1 < len(items) and items[k + 1][0] > indent else ("", k + 1))
            txt = " ".join(md_inline(x.strip(), base, repo, ref) for x in items[k][2].split("\n"))
            h.append(f"<li>{txt}{body}</li>")
            k = k2
        return "".join(h) + f"</{tag}>", k

    block, _ = build(0, items[0][0])
    out.append(block)
    return i


def md(text, base="", repo=None, ref=""):
    """最小 Markdown → HTML。コードフェンスは hl()、HTML 要素は li/table/b/blockquote/p のみ"""
    lines, out, para = text.split("\n"), [], []

    def flush():
        if para:
            out.append("<p>" + "<br>".join(md_inline(x, base, repo, ref) for x in para) + "</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        m = re.match(r"^```(\S*)\s*$", s)
        if m:
            flush()
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append('<pre class=code>' + hl("\n".join(buf), md_fence_lang(m.group(1))) + "</pre>")
            continue
        if not s:
            flush()
        elif re.match(r"^(---+|___+|\*\*\*+)$", s):
            flush()
            out.append("<hr>")
        elif re.match(r"^ {4}", lines[i]) and para:
            para.append(s)
        elif re.match(r"^#{1,6}\s", s):
            flush()
            h = s.split(" ", 1)
            lvl, txt = min(len(h[0]), 5) + 1, (h[1] if len(h) > 1 else "").strip(" #")
            out.append(f"<h{lvl} class=mdh>{md_inline(txt, base, repo, ref)}</h{lvl}>")
        elif s.startswith(">"):
            flush()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + md("\n".join(buf), base, repo, ref) + "</blockquote>")
            continue
        elif "|" in s and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}[-:\s|]*\|?\s*$", lines[i + 1]):
            flush()
            head, i = _md_row(lines[i]), i + 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_md_row(lines[i]))
                i += 1
            t = "<table><tr>" + "".join(f"<th>{md_inline(c, base, repo, ref)}</th>" for c in head) + "</tr>"
            t += "".join("<tr>" + "".join(f"<td>{md_inline(c, base, repo, ref)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(t + "</table>")
            continue
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", lines[i]):
            flush()
            i = _md_list(lines, i, out, base, repo, ref)
            continue
        else:
            para.append(s)
        i += 1
    flush()
    return "".join(out)


def diff_html(text):
    out, lang = [], ""
    for line in text.split("\n"):
        if line.startswith("+++ b/"):
            lang = hl_lang(line[6:])
        elif line.startswith("diff --git "):
            lang = ""
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            out.append(f'<span class="{"add" if line[0] == "+" else "del"}">'
                       f'{E(line[0])}{hl(line[1:], lang)}</span>')
        elif line.startswith("@@"):
            out.append(f'<span class=hunk>{E(line)}</span>')
        elif line.startswith(" ") and lang:
            out.append(" " + hl(line[1:], lang))
        else:
            out.append(E(line))
    return "<pre>" + "\n".join(out) + "</pre>"


def state_badge(s):
    return f'<span class="st {E(s)}">{E(s)}</span>'


def review_badge(cm):  # 遅延: ラベルをリクエスト言語で解決する
    k = cm["kind"] or "comment"
    lbl = {"approve": '✓ ' + t('s013'), "change": '✗ ' + t('s014')}.get(k)
    return f' <span class="st {k}">{lbl}</span>' if lbl else ""


def review_state(cms):
    """著者別の最新レビューから PR 全体の状態を判定する → ('change'|'approve'|'', 変更要求数, 承認数)"""
    latest = {}
    for cm in cms:
        if cm["kind"] in ("approve", "change"):
            latest[cm["author"]] = cm["kind"]
    vals = list(latest.values())
    if not vals:
        return "", 0, 0
    nchg, nappr = vals.count("change"), vals.count("approve")
    return ("change", nchg, nappr) if nchg else ("approve", nchg, nappr)


def author_field(user):
    return ""


# ---------------------------------------------------------------- diff (PR 画面の左右比較)
DIFF_MAX_LINES = 3000   # 1ファイルあたりの表示上限行数
DIFF_MAX_FILES = 300    # 表示する最大ファイル数
_TOKEN_RE = re.compile(r"<span class=\w+>|</span>|&\w+;|\w+|\s+|[^\w\s]", re.UNICODE)


def parse_diff(text):
    """git diff (unified) -> [{old,new,status,binary,add,del,hunks:[{header,lines:[(t,old_no,new_no,text)]}]}]"""
    files, cur, h, o, n = [], None, None, 0, 0
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            cur = {"old": None, "new": None, "status": "M", "binary": False, "add": 0, "del": 0,
                   "hunks": [], "count": 0, "truncated": False}
            m = re.match(r"^diff --git a/(.*) b/(.*)$", raw)
            if m:
                cur["old"], cur["new"] = m.group(1), m.group(2)
            files.append(cur)
            h = None
            continue
        if cur is None:
            continue
        if h is None:  # ファイルヘッダ部
            if raw.startswith("new file mode"):
                cur["status"] = "A"
            elif raw.startswith("deleted file mode"):
                cur["status"] = "D"
            elif raw.startswith("rename from "):
                cur["old"], cur["status"] = raw[12:], "R"
            elif raw.startswith("rename to "):
                cur["new"] = raw[10:]
            elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                cur["binary"] = True
            elif raw.startswith("--- ") and raw != "--- /dev/null":
                cur["old"] = raw[6:] if raw.startswith("--- a/") else raw[4:]
            elif raw.startswith("+++ ") and raw != "+++ /dev/null":
                cur["new"] = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            h = {"header": raw, "lines": []}
            cur["hunks"].append(h)
            o, n = int(m.group(1)), int(m.group(2))
            continue
        if h is None or not raw or raw[0] not in "+- ":
            continue
        t = raw[0]
        if t == "+":
            cur["add"] += 1
        elif t == "-":
            cur["del"] += 1
        if cur["count"] >= DIFF_MAX_LINES:
            cur["truncated"] = True
            continue
        cur["count"] += 1
        if t == "+":
            h["lines"].append(("+", None, n, raw[1:]))
            n += 1
        elif t == "-":
            h["lines"].append(("-", o, None, raw[1:]))
            o += 1
        else:
            h["lines"].append((" ", o, n, raw[1:]))
            o += 1
            n += 1
    return files


def inline_pair(a, b, lang=None):
    """変更行どうしの単語単位の差分を <del>/<ins> で強調（syntax 色付き）"""
    if len(a) > 500 or len(b) > 500:
        return hl(a, lang), hl(b, lang)
    ta, tb = _TOKEN_RE.findall(hl(a, lang)), _TOKEN_RE.findall(hl(b, lang))
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    if sm.ratio() < 0.35:
        return hl(a, lang), hl(b, lang)
    ra, rb = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        sa, sb = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        if op == "equal":
            ra.append(sa)
            rb.append(sb)
        else:
            if sa:
                ra.append(f"<del>{sa}</del>")
            if sb:
                rb.append(f"<ins>{sb}</ins>")
    return "".join(ra), "".join(rb)


def _blocks(lines):
    """hunk の行を (context行) / (削除群, 追加群) のブロックに分ける"""
    i = 0
    while i < len(lines):
        if lines[i][0] == " ":
            yield lines[i], None
            i += 1
            continue
        dels, adds = [], []
        while i < len(lines) and lines[i][0] == "-":
            dels.append(lines[i])
            i += 1
        while i < len(lines) and lines[i][0] == "+":
            adds.append(lines[i])
            i += 1
        yield dels, adds


def render_split(f):
    rows, lang = [], hl_lang(f["new"] or f["old"])
    empty = '<td class="n e"></td><td class="c e"></td>'
    for h in f["hunks"]:
        rows.append(f'<tr class=h><td colspan=4>{E(h["header"])}</td></tr>')
        for a, b in _blocks(h["lines"]):
            if b is None:
                _, o, n, txt = a
                cell = hl(txt, lang)
                rows.append(f"<tr><td class=n>{o}</td><td class=c>{cell}</td>"
                            f"<td class=n>{n}</td><td class=c>{cell}</td></tr>")
                continue
            for k in range(max(len(a), len(b))):
                d = a[k] if k < len(a) else None
                p = b[k] if k < len(b) else None
                if d and p:
                    dt, pt = inline_pair(d[3], p[3], lang)
                else:
                    dt, pt = (hl(d[3], lang) if d else ""), (hl(p[3], lang) if p else "")
                left = f'<td class="n del">{d[1]}</td><td class="c del">{dt}</td>' if d else empty
                right = f'<td class="n add">{p[2]}</td><td class="c add">{pt}</td>' if p else empty
                rows.append(f"<tr>{left}{right}</tr>")
    return ('<table class="d split"><colgroup><col class=nc><col class=cc><col class=nc><col class=cc></colgroup>'
            + "".join(rows) + "</table>")


def render_unified(f):
    rows, lang = [], hl_lang(f["new"] or f["old"])
    for h in f["hunks"]:
        rows.append(f'<tr class=h><td colspan=3>{E(h["header"])}</td></tr>')
        for a, b in _blocks(h["lines"]):
            if b is None:
                _, o, n, txt = a
                rows.append(f"<tr><td class=n>{o}</td><td class=n>{n}</td>"
                            f"<td class=c> {hl(txt, lang)}</td></tr>")
                continue
            pairs = [inline_pair(a[k][3], b[k][3], lang) if k < len(b) else (hl(a[k][3], lang), "") for k in range(len(a))]
            for k, d in enumerate(a):
                rows.append(f'<tr><td class="n del">{d[1]}</td><td class="n del"></td>'
                            f'<td class="c del">-{pairs[k][0]}</td></tr>')
            for k, p in enumerate(b):
                pt = pairs[k][1] if k < len(a) else hl(p[3], lang)
                rows.append(f'<tr><td class="n add"></td><td class="n add">{p[2]}</td>'
                            f'<td class="c add">+{pt}</td></tr>')
    return ('<table class="d uni"><colgroup><col class=nc><col class=nc><col class=cc></colgroup>'
            + "".join(rows) + "</table>")


STATUS_LABEL = {"A": t('s015'), "M": t('s016'), "D": t('s017'), "R": t('s018')}


def file_label(f):
    if f["status"] == "R" and f["old"] != f["new"]:
        return f'{E(f["old"])} → {E(f["new"])}'
    return E(f["new"] if f["status"] != "D" else f["old"])


# ---------------------------------------------------------------- コンフリクト解消
def conflict_info(repo, base, other):
    """merge-tree の結果。(結果tree, {path: {stage: (mode, oid)}}) / 非対応の git なら None"""
    p = git(repo, "merge-tree", "--write-tree", base, other, check=False)
    if p.returncode not in (0, 1):
        return None
    head = p.stdout.decode("utf-8", errors="replace").split("\n\n", 1)[0].split("\n")
    entries = {}
    for ln in head[1:]:
        m = re.match(r"^(\d{6}) ([0-9a-f]+) ([123])\t(.*)$", ln)
        if m:
            entries.setdefault(m.group(4), {})[int(m.group(3))] = (m.group(1), m.group(2))
    return head[0].strip(), entries


def blob(repo, oid):
    return git(repo, "cat-file", "blob", oid).stdout


def conflict_kind(st):
    if 2 in st and 3 in st:
        return t('s033') if 1 in st else t('s034')
    if 2 in st:
        return 'source ' + t('s019') + 'target ' + t('s020')
    return 'target ' + t('s019') + 'source ' + t('s020')


def resolve_conflicts(repo, src, dst, base_sha, head_sha, choices, user):
    """choices: [(path, mode, text)] mode = edit|ours|theirs|delete。
    target を source に取り込むマージコミットを作り、source ブランチを進める"""
    info = conflict_info(repo, base_sha, head_sha)
    if info is None:
        raise RuntimeError(t('s035') + ' git ' + t('s036') + ' Web ' + t('s037') + ' (git 2.38 ' + t('s038') + ')')
    tree, entries = info
    tmpdir = tempfile.mkdtemp(prefix="minigit-idx-")
    # bare リポジトリでも index 操作できるよう空の作業ツリーを指定 (ファイルは書かない)
    env = {"GIT_INDEX_FILE": os.path.join(tmpdir, "index"), "GIT_WORK_TREE": tmpdir}
    try:
        git(repo, "read-tree", tree, env=env)
        for path, mode, text in choices:
            st = entries.get(path)
            if st is None:
                raise RuntimeError(f"{path} {t('s192')}")
            pick = {"ours": 2, "theirs": 3}.get(mode)
            if mode == "delete" or (pick and pick not in st):
                git(repo, "update-index", "--force-remove", "--", path, env=env)
                continue
            if pick:
                fmode, oid = st[pick]
            else:
                text = text.replace("\r\n", "\n")
                if re.search(r"^(<{7}|>{7})( |$)", text, re.M):
                    raise RuntimeError(f"{path} {t('s224')} (<<<<<<< / >>>>>>>) {t('s225')}")
                fmode, ref_oid = st.get(3) or st.get(2)
                if b"\r\n" in blob(repo, ref_oid):  # 元ファイルが CRLF なら合わせる
                    text = text.replace("\n", "\r\n")
                data = text.encode("utf-8")
                oid = git(repo, "hash-object", "-w", "--stdin", input=data).stdout.decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"{fmode},{oid},{path}", env=env)
        new_tree = git(repo, "write-tree", env=env).stdout.decode().strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    msg = (f"Merge branch '{dst}' into {src}\n\n{t('s039')} minigit {t('s040')}:\n"
           + "".join(f"  {p} ({m})\n" for p, m, _ in choices))
    new = git(repo, "commit-tree", new_tree, "-p", head_sha, "-p", base_sha, "-m", msg,
              env=ident(user)).stdout.decode().strip()
    git(repo, "update-ref", "refs/heads/" + src, new, head_sha)  # 途中で push されていたら失敗させる
    return new


def valid_branch(repo, name):
    return bool(name) and not name.startswith("-") and ".." not in name \
        and git(repo, "check-ref-format", "refs/heads/" + name, check=False).returncode == 0


def commit_files(repo, base_sha, branch, old, path, text, msg, user):
    """1 ファイルの変更 (text=None は削除) を単一コミットして refs/heads/branch を進める。
    old は楽観ロック (その SHA から動いていなければ反映、new ブランチ作成は old="")"""
    tmpdir = tempfile.mkdtemp(prefix="minigit-edit-")
    env = {"GIT_INDEX_FILE": os.path.join(tmpdir, "index"), "GIT_WORK_TREE": tmpdir}
    try:
        git(repo, "read-tree", base_sha, env=env)
        lt = gout(repo, "ls-tree", base_sha, "--", path).strip()
        mode = lt.split()[0] if lt else "100644"
        if text is None:
            if not lt:
                raise RuntimeError(t('s136'))
            git(repo, "update-index", "--force-remove", "--", path, env=env)
        else:
            data = text.replace("\r\n", "\n").encode("utf-8")
            oid = git(repo, "hash-object", "-w", "--stdin", input=data).stdout.decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", env=env)
        tree = git(repo, "write-tree", env=env).stdout.decode().strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if text is not None and tree == gout(repo, "rev-parse", base_sha + "^{tree}").strip():
        raise RuntimeError(t('s041'))
    new = git(repo, "commit-tree", tree, "-p", base_sha, "-m", msg, env=ident(user)).stdout.decode().strip()
    args = ["update-ref", "refs/heads/" + branch, new] + ([old] if old else [])
    if git(repo, *args, check=False).returncode:
        raise RuntimeError(t('s042'))
    return new


# ---------------------------------------------------------------- ブランチ保護
def set_update_hook(repo):
    """保護設定時 (およびサーバー起動時) に update フックを作る。push 時に git が sh で実行し、
    保護ブランチへの force push / 削除を拒否する。Web からの FF コミット・PR マージは git 経由でなく許可"""
    hdir = os.path.join(repo_path(repo), "hooks")
    os.makedirs(hdir, exist_ok=True)
    path = os.path.join(hdir, "update")
    txt = ("#!/bin/sh\n"
           f"export MINIGIT_ROOT={shlex.quote(ROOT)} MINIGIT_GIT={shlex.quote(GIT)}\n"
           f"exec {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} "
           f"hook-update {shlex.quote(repo)} \"$1\" \"$2\" \"$3\"\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    os.chmod(path, 0o755)


def hook_update(repo, ref, old, new):
    """update フック本体。保護ブランチへの削除・非 fast-forward (force push) を拒否して exit 1"""
    if not ref.startswith("refs/heads/"):
        return 0
    branch = ref[len("refs/heads/"):]
    with db() as c:
        if not c.execute("SELECT 1 FROM protect WHERE repo=? AND name=?", (repo, branch)).fetchone():
            return 0
    zeros = lambda s: len(s) == 40 and not set(s) - {"0"}
    if zeros(new):
        sys.stderr.write(f"minigit: {branch} {t('s091')}\n")
        return 1
    if not zeros(old) and git(repo, "merge-base", "--is-ancestor", old, new, check=False).returncode != 0:
        sys.stderr.write(f"minigit: {branch} {t('s092')} force push ({t('s093')}) {t('s094')}\n")
        return 1
    return 0


# ---------------------------------------------------------------- handler
def ls_dir(repo, spec, ref, path):
    """ディレクトリ 1 段の行一覧と README レンダ結果を返す (GitHub 式にフォルダ先頭・名前順)"""
    entries, rd = [], None
    for line in gout(repo, "ls-tree", spec).splitlines():
        meta, name = line.split("\t", 1)
        mode, typ, oid = meta.split()
        entries.append((name, typ, oid))
        if not path and name.lower() == "readme.md":
            rd = (oid, name)
    entries.sort(key=lambda e: (e[1] != "tree", e[0].lower()))
    rows = ""
    for name, typ, oid in entries:
        p2 = (path + "/" + name).strip("/")
        label = name + "/" if typ == "tree" else name  # 絵文字フォントが無い環境でも化けない
        rows += f'<tr><td><a href="/r/{repo}/tree?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(p2)}">{E(label)}</a></td></tr>'
    box = ""
    if rd:
        oid, name = rd
        rd_txt = git(repo, "cat-file", "blob", oid).stdout.decode(errors="replace")
        rdlink = f'/r/{repo}/tree?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote((path + "/" + name).strip("/"))}'
        box = (f'<h3 class=rdh><a href="{rdlink}">{E(name)}</a></h3>'
               f'<div class=mdbox><div class=md>{md(rd_txt, path, repo, ref)}</div></div>')
    return rows, box


class H(BaseHTTPRequestHandler):
    server_version = "minigit"

    # --- auth
    me = None
    _langck = ""

    def cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def basic_user(self):
        """git 用 Basic 認証。成功なら {"name","admin"}、失敗なら 401 を送って None"""
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                raw = base64.b64decode(h[6:].strip())
                try:
                    cred = raw.decode("utf-8")
                except UnicodeDecodeError:
                    cred = raw.decode("latin-1")
                u, _, p = cred.partition(":")
            except Exception:
                u, p = "", ""
            info = authenticate(u, p) if u else None
            if info:
                return info
            sys.stderr.write(f"auth failed: user={u!r} from {self.client_address[0]}\n")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="minigit"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    # --- io
    def body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            buf = bytearray()
            while True:
                n = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if n == 0:
                    self.rfile.readline()
                    break
                buf += self.rfile.read(n)
                self.rfile.readline()
            return bytes(buf)
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def send(self, code, text, ctype="text/html; charset=utf-8", headers=None):
        if isinstance(text, str) and self.me and "<!--ME-->" in text:
            links = f'<span class=muted>{E(self.me["name"])}</span><a href="/account">{t("s095")}</a>'
            if self.me["admin"]:
                links += '<a href="/admin">' + t('s096') + '</a>'
            links += '<form method=post action="/logout" class=inline><button class="sub sm">' + t('s043') + '</button></form>'
            text = text.replace("<!--ME-->", links, 1)
        data = text.encode() if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if getattr(self, "_langck", ""):
            self.send_header("Set-Cookie", f"mg_lang={self._langck}; Path=/; Max-Age=31536000; SameSite=Lax")
            self._langck = ""
        self.end_headers()
        if self.command != "HEAD":  # HEAD にはヘッダだけ返す (Content-Length は通常通り)
            self.wfile.write(data)

    def redirect(self, url, cookie_user=None, cookie=None):
        h = {"Location": url}
        if cookie is not None:
            h["Set-Cookie"] = (f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; "
                               f"Max-Age={SESSION_DAYS * 86400 if cookie else 0}")
        self.send(303, "", headers=h)

    def form(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body().decode(), keep_blank_values=True).items()}

    # --- dispatch
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_HEAD(self):
        self.route("HEAD")  # GET と同じ扱い (body を送らない)。プロキシ等の HEAD probe 対策

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        lg = q.get("lang") if q.get("lang") in LANGS else ""
        if lg:
            self._langck = lg  # ?lang= 明示指定は Cookie に保存 (次回から网站导航でも言語が保つ)
        ck = self.cookie("mg_lang")
        lang_set(lg or (ck if ck in LANGS else "") or lang_accept(self.headers.get("Accept-Language")) or DEFAULT_LANG)
        _tl.path = (path, u.query)
        if path == "/_/style.css":
            return self.send(200, CSS_BYTES, "text/css; charset=utf-8", {"Cache-Control": "max-age=86400"})
        self.q = q
        # git (clone/push) は Basic 認証
        m = re.match(r"^/([^/]+)\.git(/.*)?$", path)
        if m and NAME_RE.match(m.group(1)) and os.path.isdir(repo_path(m.group(1))):
            me = self.basic_user()
            if me is None:
                return
            try:
                return self.cgi(path, u.query, me["name"])
            except Exception as ex:
                return self.send(500, str(ex), "text/plain; charset=utf-8")
        # 画面はログインフォーム + セッション Cookie
        if method == "POST" and not self.same_origin():
            return self.send(403, page("403", '<h2>' + t('s137') + '</h2>'))
        if path == "/login":
            return self.login_post(self.form()) if method == "POST" else self.login_page(q)
        if path == "/logout":
            tok = self.cookie(SESSION_COOKIE)
            if tok:
                delete_session(tok)
            return self.redirect("/login?msg=" + urllib.parse.quote(t('s193')), cookie="")
        me = session_user(self.cookie(SESSION_COOKIE))
        if me is None:
            nxt = self.path if method != "POST" else "/"
            return self.redirect("/login?next=" + urllib.parse.quote(nxt))
        self.me = me
        user = me["name"]
        try:
            if path == "/admin":
                if not me["admin"]:
                    return self.send(403, page("403", '<h2>' + t('s226') + '</h2>'))
                return self.admin_post(self.form()) if method == "POST" else self.admin_page(q)
            if path == "/account":
                return self.account_post(self.form()) if method == "POST" else self.account_page(q)
            if path == "/" and method != "POST":
                return self.index()
            if path == "/new" and method == "POST":
                return self.create_repo(self.form())
            m = re.match(r"^/r/([^/]+)(/.*)?$", path)
            if m and NAME_RE.match(m.group(1)) and os.path.isdir(repo_path(m.group(1))):
                return self.repo_route(method, m.group(1), m.group(2) or "/", q, user)
            if m:
                return self.send(404, page("404", f"<h2>{t('s227')}</h2>"
                                                  f"<p>{t('s228')}: <code>{E(repo_path(m.group(1)))}</code></p>"))
            self.send(404, page("404", "<h2>Not Found</h2>"))
        except Exception as ex:
            self.send(500, page("error", f"<h2>Error</h2><pre>{E(str(ex))}</pre>"))

    # --- git smart http
    def cgi(self, path, query, user):
        body = self.body() if self.command == "POST" else b""
        env = {
            "GIT_PROJECT_ROOT": ".", "GIT_HTTP_EXPORT_ALL": "1",  # ROOT は cwd で渡す
            "PATH_INFO": path, "REQUEST_METHOD": self.command, "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": user or "anonymous", "REMOTE_ADDR": self.client_address[0],
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
        }
        p = run([GIT, "http-backend"], input=body, env=env, check=False, cwd=ROOT)
        out = p.stdout
        sep = out.find(b"\r\n\r\n")
        hdr, data = (out[:sep], out[sep + 4:]) if sep >= 0 else out.split(b"\n\n", 1)
        code, headers = 200, {}
        for line in hdr.decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                code = int(v.strip().split()[0])
            elif k:
                headers[k.strip()] = v.strip()
        ctype = headers.pop("Content-Type", "application/octet-stream")
        self.send(code, data, ctype, headers)

    # --- top
    def index(self):
        rows = "".join(f'<tr><td><a href="/r/{urllib.parse.quote(r)}">{E(r)}</a></td></tr>' for r in repos())
        self.send(200, page("minigit", f"""<h2>Repositories</h2><table>{rows or '<tr><td>' + t('s229') + '</td></tr>'}</table>
<div class=box><form method=post action=/new><b>{t('s138')}</b>
<input type=text name=name placeholder="name ({t('s139')} . _ -)"><button>{t('s057')}</button></form></div>"""))

    def create_repo(self, f):
        name = f.get("name", "").strip()
        if not NAME_RE.match(name) or os.path.exists(repo_path(name)):
            return self.send(400, page("error", '<h2>' + t('s140') + '</h2>'))
        rp = repo_path(name)
        os.makedirs(rp)  # フォルダは Python 側で作り、git にはパスを渡さず cwd で指定する
        p = run([GIT, "init", "--bare", "."], cwd=rp)
        if not os.path.isfile(os.path.join(rp, "HEAD")):
            msg = (p.stdout + p.stderr).decode(errors="replace").strip()
            shutil.rmtree(rp, ignore_errors=True)
            raise RuntimeError(f"git init {t('s141')} {rp} {t('s142')}\n"
                               f"git {t('s143')}: {msg}\nPython: {sys.executable}\ngit: {GIT}")
        git(name, "symbolic-ref", "HEAD", "refs/heads/main")
        git(name, "config", "http.receivepack", "true")
        self.redirect("/r/" + urllib.parse.quote(name))

    # --- repo pages
    def repo_route(self, method, repo, sub, q, user):
        if sub in ("/", ""):
            return self.code(repo, q)
        if sub == "/tree":
            return self.tree(repo, q)
        if sub == "/branches":
            return self.branch_post(repo, self.form()) if method == "POST" else self.branch_page(repo, q)
        if sub == "/commit":
            return self.commit(repo, q)
        if sub == "/edit":
            return self.edit_post(repo, self.form()) if method == "POST" else self.edit_page(repo, q)
        if sub in ("/issues", "/pulls"):
            kind = "issue" if sub == "/issues" else "pr"
            return self.create_item(repo, kind, self.form()) if method == "POST" else self.list_items(repo, kind, q, user)
        m = re.match(r"^/i/(\d+)/resolve$", sub)
        if m:
            num = int(m.group(1))
            with db() as c:
                it = c.execute("SELECT * FROM item WHERE repo=? AND num=? AND kind='pr'", (repo, num)).fetchone()
            if not it:
                return self.send(404, page("404", "<h2>Not Found</h2>", repo))
            return self.resolve_post(repo, num, it, self.form()) if method == "POST" else self.resolve_page(repo, num, it)
        m = re.match(r"^/i/(\d+)$", sub)
        if m:
            num = int(m.group(1))
            return self.item_action(repo, num, self.form(), user) if method == "POST" else self.item(repo, num, user)
        self.send(404, page("404", "<h2>Not Found</h2>", repo))

    def code(self, repo, q):
        host = self.headers.get("Host", "localhost")
        url = f"http://{host}/{repo}.git"
        brs = branches(repo)
        if not brs:
            return self.send(200, page(repo, f"""<h2>{E(repo)}</h2><p>{t('s194')}</p>
<h3>{t('s195')}</h3>
<pre>git init
git remote add origin {E(url)}
git add -A
git commit -m "initial commit"
git branch -M main
git push -u origin main</pre>
<h3>clone {t('s196')}</h3>
<pre>git clone {E(url)}</pre>""", repo))
        hb = head_branch(repo)
        ref = q.get("ref") or (hb if hb in brs else brs[0])
        sha = resolve(repo, ref)
        opts = "".join(f'<option{" selected" if b == ref else ""}>{E(b)}</option>' for b in brs)
        log = gout(repo, "log", "-n", "30", "--format=%H%x09%h%x09%an%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M", sha)
        rows = ""
        for line in log.splitlines():
            h, short, an, ad, s = line.split("\t", 4)
            rows += f'<tr><td><a href="/r/{repo}/commit?id={h}"><code>{short}</code></a></td><td>{E(s)}</td><td>{E(an)}</td><td>{ad}</td></tr>'
        files, box = ls_dir(repo, f"{sha}^{{tree}}", ref, "")  # GitHub 式: トップからファイル一覧+README
        nbtn = f'<a class="btn sub" href="/r/{repo}/edit?ref={urllib.parse.quote(ref)}&p=">➕ {t("s044")}</a>'
        self.send(200, page(repo, f"""<h2>{E(repo)}</h2>
<div class=clonebar><form method=get class=inline><select name=ref onchange="this.form.submit()">{opts}</select></form>
<pre>git clone <span id=mgclone>{E(url)}</span></pre>
<button class="btn sub" onclick="mgC(this)" data-t=mgclone>{t('s144')}</button></div>
<h3>{t('s063')} {nbtn}</h3><table>{files}</table>{box}
<h3>Commits ({E(ref)})</h3><div class=scrollbox><table>{rows}</table></div>""", repo))

    def tree(self, repo, q):
        ref, path = q.get("ref", ""), q.get("p", "").strip("/")
        sha = resolve(repo, ref)
        if not sha or ".." in path.split("/"):
            return self.send(404, page("404", "<h2>Not Found</h2>", repo))
        spec = f"{sha}:{path}" if path else f"{sha}^{{tree}}"
        obj = git(repo, "cat-file", "-t", spec, check=False).stdout.decode().strip()  # obj: t だと翻訳関数 t() を隠蔽してしまう
        crumbs = f'<a href="/r/{repo}/tree?ref={urllib.parse.quote(ref)}">{E(ref)}</a>'
        acc = ""
        for part in [x for x in path.split("/") if x]:
            acc = (acc + "/" + part).strip("/")
            crumbs += f' / <a href="/r/{repo}/tree?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(acc)}">{E(part)}</a>'
        msg = f'<div class=msg>{E(q["msg"])}</div>' if q.get("msg") else ""
        if obj == "tree":
            rows, box = ls_dir(repo, spec, ref, path)
            nbtn = f'<a class="btn sub" href="/r/{repo}/edit?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(path + "/")}">➕ {t("s044")}</a>'
            body = f"{msg}<h3>{crumbs} {nbtn}</h3><table>{rows}</table>{box}"
        elif obj == "blob":
            data = git(repo, "cat-file", "blob", spec).stdout
            binary = b"\0" in data[:8000]
            if q.get("raw") == "1" and not binary:
                ext = path.rpartition(".")[2].lower()
                ct = RAW_TYPES.get(ext, "text/plain")
                if ct.startswith("text/"):
                    ct += "; charset=utf-8"  #charset 不在りだとブラウザが日本語を化かす
                return self.send(200, data, ct)
            txt = '(' + t('s145') + ')' if binary else data.decode(errors="replace")
            ebtn = "" if binary else f'<a class="btn sub" href="/r/{repo}/edit?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(path)}">✏️ {t("s197")}</a>'
            rawq = f"/r/{repo}/tree?{urllib.parse.urlencode({'ref': ref, 'p': path, 'raw': '1'})}"
            cbtn = "" if binary else '<button class="btn sub" onclick="mgC(this)">' + t('s144') + '</button>'
            if not binary and path.lower().endswith((".md", ".markdown")):
                view = f'<a class="btn sub" href="{rawq}">Raw</a>'
                body = f"{msg}<h3>{crumbs} {ebtn} {cbtn.replace('>', f' data-u=\"{E(rawq, quote=True)}\">', 1)} {view}</h3><div class=md>{md(txt, path.rpartition('/')[0], repo, ref)}</div>"
            else:
                pre = '<pre id=mgpre>' + hl(txt, hl_lang(path)) + "</pre>" if not binary else '<pre>(' + t('s145') + ')</pre>'
                body = f"{msg}<h3>{crumbs} {ebtn} {cbtn}</h3>{pre}"
        else:
            return self.send(404, page("404", "<h2>Not Found</h2>", repo))
        self.send(200, page(repo, body, repo))

    def branch_page(self, repo, q):
        hb = head_branch(repo)
        base = resolve(repo, hb)
        with db() as c:
            used = {}
            for r in c.execute("SELECT num, src, dst FROM item WHERE repo=? AND kind='pr' AND state='open'", (repo,)):
                used.setdefault(r["src"], []).append(r["num"])
                used.setdefault(r["dst"], []).append(r["num"])
            prot = {r["name"] for r in c.execute("SELECT name FROM protect WHERE repo=?", (repo,))}
        msg = f'<div class=msg>{E(q["msg"])}</div>' if q.get("msg") else ""
        out = gout(repo, "for-each-ref", "--sort=-committerdate",
                   "--format=%(refname:short)%09%(objectname)%09%(committerdate:format:%Y-%m-%d %H:%M)%09%(authorname)%09%(subject)",
                   "refs/heads")
        trs = ""
        for line in out.splitlines():
            name, sha, date, an, subj = (line.split("\t", 4) + [""] * 5)[:5]
            shield = ' <span title="' + t('s097') + ': push ' + t('s098') + ' force push' + t('s099') + '">🛡</span>' if name in prot else ""
            pbtn = (f'<form method=post class=inline><input type=hidden name=name value="{E(name)}">'
                    f'<button class=sub name=action value="{"unprotect" if name in prot else "protect"}">'
                    f'{t('s198') if name in prot else t('s199')}</button></form>')
            merged = bool(base) and git(repo, "merge-base", "--is-ancestor", sha, base, check=False).returncode == 0
            if name == hb:
                state, btn = '<b>' + t('s146') + '</b>', ""
            elif name in used:
                prs = " ".join(f'<a href="/r/{repo}/i/{n}">#{n}</a>' for n in used[name])
                state, btn = f"{t('s230')} PR {prs}", ""
            else:
                state = t('s022') if merged else '<span class=warn>' + t('s200') + '</span>'
                conf = "" if merged or name in prot else ' onclick="return confirm(&quot;' + t('s201') + '&quot;)"'
                btn = "" if name in prot else (
                    f'<form method=post class=inline><input type=hidden name=name value="{E(name)}">'
                    f'<button class=danger name=action value=delete{conf}>{t("s017")}</button></form>')
            trs += (f'<tr><td><a href="/r/{repo}?ref={urllib.parse.quote(name)}"><b>{E(name)}</b></a>{shield}</td>'
                    f'<td><small>{E(subj)}<br>{E(an)} {date}</small></td><td>{state}</td><td>{btn} {pbtn}</td></tr>')
        self.send(200, page("branches", f"""<h2>Branches</h2>{msg}<table>{trs or '<tr><td>' + t('s229') + '</td></tr>'}</table>
<p><small>{t('s147')}: <code>git push origin --delete {t('s148')}</code> · {t('s149')} push {t('s098')}
force push {t('s150')} (PR {t('s151')} commit {t('s152')})</small></p>""", repo))

    def branch_post(self, repo, f):
        name = f.get("name", "")
        back = f.get("back", "")
        back = back if back.startswith(f"/r/{repo}/") else f"/r/{repo}/branches"
        p = git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + name, check=False) \
            if name and not name.startswith("-") else None
        sha = p.stdout.decode().strip() if p is not None and p.returncode == 0 else ""
        act = f.get("action", "")
        if act == "protect" and sha:
            with db() as c:
                c.execute("INSERT OR IGNORE INTO protect(repo,name) VALUES(?,?)", (repo, name))
            set_update_hook(repo)
            msg = f"{t('s100')} {name} {t('s101')} (push {t('s098')} force push{t('s099')})"
        elif act == "unprotect":
            with db() as c:
                c.execute("DELETE FROM protect WHERE repo=? AND name=?", (repo, name))
            msg = f"{t('s100')} {name} {t('s153')}"
        elif act != "delete" or not sha:
            msg = t('s154')
        elif name == head_branch(repo):
            msg = t('s202')
        else:
            with db() as c:
                protected = c.execute("SELECT 1 FROM protect WHERE repo=? AND name=?", (repo, name)).fetchone()
                prs = [r["num"] for r in c.execute(
                    "SELECT num FROM item WHERE repo=? AND kind='pr' AND state='open' AND (src=? OR dst=?)",
                    (repo, name, name))]
            if protected:
                msg = f"{t('s238')} ({t('s239')})"
            elif prs:
                msg = f"{t('s230')} PR ({', '.join('#' + str(n) for n in prs)}) {t('s246')}"
            else:
                git(repo, "update-ref", "-d", "refs/heads/" + name, sha)
                msg = f"{t('s100')} {name} {t('s106')} ({t('s247')} {sha[:10]})"
                sys.stderr.write(f"branch deleted: {repo} {name} {sha} by {self.me['name']}\n")
        sep = "&" if "?" in back else "?"
        self.redirect(back + sep + "msg=" + urllib.parse.quote(msg))

    def commit(self, repo, q):
        cid = q.get("id", "")
        if not SHA_RE.match(cid):
            return self.send(400, page("error", "bad id", repo))
        out = gout(repo, "show", "--stat", "-p", "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n%B", cid)
        self.send(200, page(repo, f"<h2>Commit {E(cid[:10])}</h2>" + diff_html(out), repo))

    # --- Web ファイル編集
    def edit_page(self, repo, q):
        ref, path = q.get("ref", ""), q.get("p", "").strip("/")
        msg = f'<div class="msg err">{E(q["msg"])}</div>' if q.get("msg") else ""
        sha = resolve(repo, ref)
        if not sha or ".." in path.split("/"):
            return self.send(404, page("404", "<h2>Not Found</h2>", repo))
        exists = bool(path) and git(repo, "cat-file", "-t", f"{sha}:{path}",
                                    check=False).stdout.decode().strip() == "blob"
        data = git(repo, "cat-file", "blob", f"{sha}:{path}").stdout if exists else b""
        if exists and (b"\0" in data[:8000] or len(data) > 512 * 1024):
            return self.send(200, page(repo, f"{msg}<div class='msg err'>{t('s203')}512KB {t('s103')} Web {t('s104')}</div>", repo))
        txt = data.decode(errors="replace") if exists else ""
        brs = gout(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
        opts = "".join(f'<option{" selected" if b == ref else ""}>{E(b)}</option>' for b in brs)
        pathin = (f'<input type=hidden name=p value="{E(path)}">' if exists else
                  '<p><label>' + t('s045') + ' <input name=p size=50 placeholder="' + t('s046') + ': docs/README.md" required></label></p>')
        body = f"""{msg}<h3>{E(path) if exists else t('s044')}</h3>
<form method=post action="/r/{repo}/edit">
<input type=hidden name=ref value="{E(ref)}">
{pathin}
<p><textarea name=text class=code spellcheck=false rows={min(40, txt.count(chr(10)) + 3)} placeholder="{t('s047')}">{E(txt)}</textarea></p>
<p><label>{t('s048')} <select name=branch>{opts}</select>
<label> {t('s049')} <input name=newbranch size=24 placeholder="({t('s050')}={t('s051')})"></label></label></p>
<p><label>{t('s052')} <input name=msg size=60 required
 placeholder="{E(('Update ' if exists else 'Add ') + (path or ''))}"></label></p>
<p><button name=action value=commit class=ok>{t('s053')}</button>
{"<button name=action value=delete class=danger onclick=&quot;return confirm('" + t('s155') + "?')&quot;>" + t('s156') + '</button>' if exists else ""}</p>
</form>"""
        self.send(200, page(repo, body, repo, wide=True))

    def edit_post(self, repo, f):
        ref, path = f.get("ref", ""), (f.get("p", "") or "").strip("/")
        back = f"/r/{repo}/edit?ref={urllib.parse.quote(ref)}&p={urllib.parse.quote(path)}"

        def fail(m):
            return self.redirect(back + "&msg=" + urllib.parse.quote(m))

        if not path or ".." in path.split("/") or len(path.encode("utf-8")) > 512:
            return fail(t('s102'))
        act = f.get("action", "commit")
        text = None if act == "delete" else f.get("text", "")
        if text is not None and len(text.encode("utf-8")) > 512 * 1024:
            return fail('512KB ' + t('s103') + ' Web ' + t('s104'))
        newb = f.get("newbranch", "").strip()
        if newb:
            branch, base, old = newb, resolve(repo, ref), ""
            if not valid_branch(repo, branch):
                return fail(t('s157'))
            if not base:
                return fail(t('s158'))
            if resolve(repo, branch):
                return fail(t('s159'))
        else:
            branch = f.get("branch", "") or (ref if valid_branch(repo, ref) else "")
            if not valid_branch(repo, branch):
                return fail(t('s160'))
            base = old = resolve(repo, branch)
            if not base:
                return fail(t('s154'))
        msg = (f.get("msg", "").strip() or ("Delete " if text is None else "Update ") + path)
        try:
            new = commit_files(repo, base, branch, old, path, text, msg, self.me["name"])
        except Exception as ex:
            return fail(str(ex))
        done = (t('s105') + ' ' + new[:8]) if text is not None else f"{path} {t('s106')}"
        url = (f"/r/{repo}/tree?ref={urllib.parse.quote(branch)}&p={urllib.parse.quote(path)}"
               if text is not None else f"/r/{repo}/tree?ref={urllib.parse.quote(branch)}")
        return self.redirect(url + "&msg=" + urllib.parse.quote(done))

    # --- issues / PRs
    def list_items(self, repo, kind, q, user):
        st = q.get("state", "open")
        with db() as c:
            rows = c.execute("SELECT * FROM item WHERE repo=? AND kind=? AND (state=? OR ?='all') ORDER BY num DESC",
                             (repo, kind, st, st)).fetchall()
        label = "Issues" if kind == "issue" else "Pull Requests"
        base = f"/r/{repo}/{'issues' if kind == 'issue' else 'pulls'}"
        trs = "".join(
            f'<tr><td>{state_badge(r["state"])}</td><td><a href="/r/{repo}/i/{r["num"]}">#{r["num"]} {E(r["title"])}</a>'
            + (f' <small>{E(r["src"])} → {E(r["dst"])}</small>' if kind == "pr" else "")
            + f'</td><td>{E(r["author"])}</td><td>{r["created"]}</td></tr>' for r in rows)
        if kind == "pr":
            brs = branches(repo)
            hb = head_branch(repo)
            so = "".join(f"<option>{E(b)}</option>" for b in brs)
            do = "".join(f'<option{" selected" if b == hb else ""}>{E(b)}</option>' for b in brs)
            extra = f"<label>from (source)<select name=src>{so}</select></label><label>into (target)<select name=dst>{do}</select></label>"
        else:
            extra = ""
        form = f"""<div class=box><form method=post action="{base}"><b>{t('s054')} {label[:-1]}</b>{extra}
<input type=text name=title placeholder="{t('s055')}" required><textarea name=body placeholder="{t('s056')}"></textarea>
{author_field(user)}<button>{t('s057')}</button></form></div>"""
        tabs = " ".join(f'<a href="{base}?state={s}">{"<b>" + s + "</b>" if s == st else s}</a>'
                        for s in (["open", "closed", "all"] + (["merged"] if kind == "pr" else [])))
        self.send(200, page(label, f"<h2>{label}</h2><div class=tabs>{tabs}</div><table>{trs or '<tr><td>' + t('s229') + '</td></tr>'}</table>{form}", repo))

    def create_item(self, repo, kind, f):
        user = self.user_from(f)
        title = f.get("title", "").strip()
        if not title:
            return self.send(400, page("error", t('s161'), repo))
        src = dst = None
        if kind == "pr":
            src, dst = f.get("src", ""), f.get("dst", "")
            if src == dst or not resolve(repo, src) or not resolve(repo, dst):
                return self.send(400, page("error", '<h2>source/target ' + t('s204') + '</h2>', repo))
        with db() as c:
            num = (c.execute("SELECT MAX(num) FROM item WHERE repo=?", (repo,)).fetchone()[0] or 0) + 1
            c.execute("INSERT INTO item(repo,num,kind,title,body,author,src,dst,created) VALUES(?,?,?,?,?,?,?,?,?)",
                      (repo, num, kind, title, f.get("body", ""), user, src, dst, now()))
        self.redirect(f"/r/{repo}/i/{num}", user)

    def user_from(self, f):
        return self.me["name"]

    def item(self, repo, num, user):
        with db() as c:
            it = c.execute("SELECT * FROM item WHERE repo=? AND num=?", (repo, num)).fetchone()
            cms = c.execute("SELECT * FROM comment WHERE repo=? AND num=? ORDER BY id", (repo, num)).fetchall()
        if not it:
            return self.send(404, page("404", "<h2>Not Found</h2>", repo))
        if it["kind"] == "pr":
            return self.pr_view(repo, num, it, cms, user)
        h = f'<div class=msg>{E(self.q["msg"])}</div>' if self.q.get("msg") else ""
        h += f'<h2>{E(it["title"])} <small>#{num}</small></h2><p>{state_badge(it["state"])} {E(it["author"])} {t("s058")} {it["created"]} {t("s059")}</p>'
        body_html = f'<pre>{E(it["body"])}</pre>' if (it["body"] or "").strip() else '<p class=muted>(' + t('s060') + ')</p>'
        h += f'<div class=box><b>{E(it["author"])}</b>{body_html}</div>'
        for cm in cms:
            h += f'<div class=box><b>{E(cm["author"])}</b>{review_badge(cm)} <small>{cm["created"]}</small><pre>{E(cm["body"])}</pre></div>'
        toggle = ""
        if it["state"] == "open":
            toggle = "<button class=sub name=action value=close>Close</button>"
        elif it["state"] == "closed":
            toggle = "<button class=sub name=action value=reopen>Reopen</button>"
        h += f"""<div class=box><form method=post><textarea name=body placeholder="{t('s061')}"></textarea>
{author_field(user)}<button name=action value=comment>Comment</button> {toggle}</form></div>"""
        self.send(200, page(f"#{num}", h, repo))

    # --- PR 画面 (左: ファイル/コミット一覧, 右: 左右比較の差分)
    def pr_view(self, repo, num, it, cms, user):
        src, dst = it["src"], it["dst"]
        view = "unified" if self.q.get("view") == "unified" else "split"
        s, d = resolve(repo, src), resolve(repo, dst)
        base_url = f"/r/{repo}/i/{num}"

        files, commits, truncated_files = [], [], 0
        if s and d:
            mb = merge_base(repo, d, s)
            raw = git(repo, "diff", "-M", "--no-color", "--no-ext-diff", "-U3", mb or d, s).stdout
            files = parse_diff(raw.decode("utf-8", errors="replace"))
            if len(files) > DIFF_MAX_FILES:
                truncated_files = len(files) - DIFF_MAX_FILES
                files = files[:DIFF_MAX_FILES]
            for line in gout(repo, "log", "-n", "200", "--format=%H%x09%h%x09%an%x09%s", f"{d}..{s}").splitlines():
                commits.append(line.split("\t", 3))
        adds, dels = sum(f["add"] for f in files), sum(f["del"] for f in files)

        msg = f'<div class=msg>{E(self.q["msg"])}</div>' if self.q.get("msg") else ""
        h = f"""{msg}<div class=pr-head><h2>{E(it["title"])} <small>#{num}</small></h2>
<div class=pr-meta>{state_badge(it["state"])} <b>{E(it["author"])}</b> {t('s058')}
<code>{E(src)}</code> → <code>{E(dst)}</code> {t('s062')} <span class=muted>· {it["created"]}</span>
<span class=muted>· {len(commits)} {t('s053')} · {len(files)} {t('s063')}</span>
<span class=a>+{adds}</span> <span class=r>−{dels}</span></div></div>"""

        rv, nchg, nappr = review_state(cms)
        if it["state"] == "open" and rv:
            label = (f'<span class="st approve">✓ {t("s162")} ({nappr})</span>' if rv == "approve"
                     else f'<span class="st change">✗ {t("s163")} ({nchg})</span>')
            h += f'<p>{t("s107")}: {label} <small class=muted>{t("s108")} ({t("s109")})</small></p>'

        # --- マージ状況バー
        if it["state"] == "merged":
            bar = (f'<div class="mergebar merged"><div><b>{t("s022")}</b> {t("s110")} '
                   f'<a href="/r/{repo}/commit?id={it["merge_sha"]}"><code>{(it["merge_sha"] or "")[:10]}</code></a></div>')
            if s and src != head_branch(repo):
                bar += (f'<form method=post action="/r/{repo}/branches" class=inline><input type=hidden name=name value="{E(src)}">'
                        f'<input type=hidden name=back value="{base_url}">'
                        f'<button class="sub sm" name=action value=delete>{t("s100")} {E(src)} {t("s164")}</button></form>')
            bar += "</div>"
        elif it["state"] == "closed":
            bar = '<div class="mergebar closed"><div><b>' + t('s111') + '</b> ' + t('s035') + ' PR ' + t('s112') + '</div></div>'
        else:
            ok, why = mergeable(repo, src, dst, user)
            if ok:
                bar = (f'<div class="mergebar ok"><div><b>✓ {t("s205")}</b> <span class=muted>'
                       f'{E(src)} {t("s072")} {E(dst)} {t("s206")}</span></div>'
                       f'<form method=post class=inline><button class=ok name=action value=merge>Merge</button></form></div>')
            elif why.startswith(t('s207')):
                files_c = [ln.strip() for ln in why.split("\n")[2:] if ln.startswith("  ") and not ln.startswith("  git")
                           and not ln.startswith("  (")]
                can_web = s and d and conflict_info(repo, d, s) is not None
                btn = (f'<a class="btn ok" href="{base_url}/resolve">Web {t("s240")}</a>' if can_web else "")
                bar = (f'<div class="mergebar ng"><div><b>⚠ {t("s080")}</b> <span class=muted>'
                       f'{E(", ".join(files_c[:5]))}{' ' + t('s116') if len(files_c) > 5 else ""}</span>'
                       f'<details class=howto><summary>{t("s231")}</summary><pre>git checkout {E(src)}\n'
                       f'git pull origin {E(dst)}\n({t("s084")})\ngit push</pre></details></div>{btn}</div>')
            else:
                bar = f'<div class="mergebar ng"><div><b>⚠ {E(why.splitlines()[0])}</b><pre>{E(why)}</pre></div></div>'
        h += bar

        # --- 左ペイン
        side = f'<div class=side-h>{t("s064")} <span class=muted>{len(files)}</span></div><ul class=flist>'
        for i, f in enumerate(files):
            path = f["new"] if f["status"] != "D" else f["old"]
            dname, _, bname = (path or "").rpartition("/")
            side += (f'<li><a href="#f{i}" title="{E(path or "")}"><span class="fs s{f["status"]}">{f["status"]}</span>'
                     f'<span class=fn>{E(bname)}<small>{E(dname)}</small></span>'
                     f'<span class=fc><span class=a>+{f["add"]}</span> <span class=r>−{f["del"]}</span></span></a></li>')
        side += f'</ul><div class=side-h>{t("s053")} <span class=muted>{len(commits)}</span></div><ul class=clist>'
        for full, short, an, subj in commits:
            side += f'<li><a href="/r/{repo}/commit?id={full}"><code>{short}</code> {E(subj)}</a><small>{E(an)}</small></li>'
        side += "</ul>"

        # --- 右ペイン
        sw = (f'<div class=viewsw>{t("s065")}: <a href="{base_url}?view=split" class="{"on" if view == "split" else ""}">{t("s066")}</a>'
              f'<a href="{base_url}?view=unified" class="{"on" if view == "unified" else ""}">{t("s067")}</a></div>')
        legend = (f'<div class=legend><span>◀ {t("s113")} <code>{E(dst)}</code></span><span>{t("s114")} <code>{E(src)}</code> ▶</span></div>'
                  if view == "split" else "")
        body = ""
        for i, f in enumerate(files):
            if f["binary"]:
                content = '<p class="muted pad">' + t('s115') + '</p>'
            elif not f["hunks"]:
                content = '<p class="muted pad">' + t('s165') + ' (' + t('s166') + ')</p>'
            else:
                content = render_split(f) if view == "split" else render_unified(f)
                if f["truncated"]:
                    content += f'<p class="muted pad">{t("s232")} {DIFF_MAX_LINES} {t("s233")}</p>'
            body += (f'<details class=file id=f{i} open><summary><span class="fs s{f["status"]}">{f["status"]}</span>'
                     f'<span class=path>{file_label(f)}</span><span class=fc><span class=a>+{f["add"]}</span> '
                     f'<span class=r>−{f["del"]}</span></span></summary>{content}</details>')
        if truncated_files:
            body += f'<p class=muted>{t("s116")} {truncated_files} {t("s117")}</p>'
        if not files:
            body += '<p class=muted>' + t('s068') + '</p>'
        h += (f'<div class=pr-layout><div class=pr-side>{side}</div>'
              f'<div class=pr-main>{sw}{legend}{body}</div></div>')

        # --- 会話
        body_html = (f'<pre class=txt>{E(it["body"])}</pre>' if (it["body"] or "").strip()
                     else '<p class=muted>(' + t('s060') + ')</p>')
        h += f'<h3 class=conv>{t("s069")}</h3><div class=box><b>{E(it["author"])}</b> <small class=muted>{it["created"]}</small>{body_html}</div>'
        for cm in cms:
            body_txt = f'<pre class=txt>{E(cm["body"])}</pre>' if (cm["body"] or "").strip() else ""
            h += (f'<div class=box><b>{E(cm["author"])}</b>{review_badge(cm)} <small class=muted>{cm["created"]}</small>'
                  f'{body_txt}</div>')
        toggle = ""
        revbtns = ""
        if it["state"] == "open":
            toggle = "<button class=sub name=action value=close>Close</button>"
            revbtns = ('<button class=ok name=action value=approve>✓ ' + t('s013') + '</button> <button class=danger name=action value=change>✗ ' + t('s014') + '</button> ')
        elif it["state"] == "closed":
            toggle = "<button class=sub name=action value=reopen>Reopen</button>"
        h += (f'<div class=box><form method=post><textarea name=body placeholder="{t("s061")} ({t("s070")})"></textarea>'
              f'<button name=action value=comment>Comment</button> {revbtns}{toggle}</form></div>')
        self.send(200, page(f"#{num} {it['title']}", h, repo, wide=True))

    # --- コンフリクト解消画面
    def resolve_page(self, repo, num, it, err=""):
        src, dst = it["src"], it["dst"]
        s, d = resolve(repo, src), resolve(repo, dst)
        # 表示用: マーカーにブランチ名が出るよう名前で実行 (確定時は SHA で再計算・検証する)
        info = conflict_info(repo, dst, src) if s and d and merge_base(repo, d, s) else None
        back = f"/r/{repo}/i/{num}"
        if it["state"] != "open" or info is None or not info[1]:
            reason = (t('s035') + ' git ' + t('s036') + ' Web ' + t('s037') + ' (git 2.38 ' + t('s038') + ')' if s and d and info is None
                      and merge_base(repo, d, s) else t('s118'))
            return self.redirect(back + "?msg=" + urllib.parse.quote(reason))
        tree, entries = info
        h = f'<div class="msg err">{E(err)}</div>' if err else ""
        h += (f'<div class=pr-head><h2>{t("s071")} <small>#{num} {E(it["title"])}</small></h2>'
              f'<div class=pr-meta><code>{E(dst)}</code> {t("s072")} <code>{E(src)}</code> {t("s073")}'
              f'<code>{E(src)}</code> {t("s074")} PR {t("s075")} Merge {t("s076")}</div></div>'
              f'<form method=post><input type=hidden name=base value="{d}"><input type=hidden name=head value="{s}">'
              f'<input type=hidden name=n value="{len(entries)}">')
        for i, (path, st) in enumerate(sorted(entries.items())):
            sides = {k: (blob(repo, st[k][1]) if k in st else None) for k in (2, 3)}
            binary = any(v is not None and b"\0" in v[:8000] for v in sides.values())
            try:
                merged = git(repo, "cat-file", "blob", f"{tree}:{path}", check=False).stdout
            except Exception:
                merged = b""
            dec = lambda b: b.decode("utf-8", errors="replace").replace("\r\n", "\n") if b is not None else None
            left, right = dec(sides[2]), dec(sides[3])
            edit_default = not binary
            opt = lambda v, label, on: (f'<label><input type=radio name=mode{i} value={v}{" checked" if on else ""}>'
                                        f' {label}</label>')
            opts = ""
            if not binary:
                opts += opt("edit", t('s167'), True)
            opts += opt("ours", f"target {t('s208')} ({E(dst)}) {t('s209')}" + (t('s210') if left is None else ""), not edit_default and left is not None)
            opts += opt("theirs", f"source {t('s208')} ({E(src)}) {t('s209')}" + (t('s210') if right is None else ""), not edit_default and left is None)
            opts += opt("delete", t('s119'), False)
            pane = lambda label, txt: (f'<div class=rpane><div class=rpl>{label}</div>'
                                       + (f'<pre>{E(txt)}</pre>' if txt is not None else '<p class="muted pad">(' + t('s234') + ')</p>')
                                       + '</div>')
            h += (f'<div class=rfile><div class=rhead><span class=path>{E(path)}</span>'
                  f'<span class=muted>{conflict_kind(st)}</span></div>'
                  f'<input type=hidden name=path{i} value="{E(path)}">'
                  f'<div class=rcols>{pane("◀ target: " + E(dst), '(' + t('s235') + ')' if binary and left is not None else left)}'
                  f'{pane("source: " + E(src) + " ▶", '(' + t('s235') + ')' if binary and right is not None else right)}</div>'
                  f'<div class=ropts>{opts}</div>')
            if not binary:
                nrows = min(40, max(8, merged.count(b"\n") + 2))
                h += (f'<textarea name=text{i} class=code spellcheck=false rows="{nrows}">'
                      f'\n{E(dec(merged) or "")}</textarea>'
                      f'<p class=hint>&lt;&lt;&lt;&lt;&lt;&lt;&lt; {t("s089")} &gt;&gt;&gt;&gt;&gt;&gt;&gt; {t("s168")}</p>')
            h += "</div>"
        h += (f'<div class=mergebar><div>{t("s077")}</div>'
              f'<div><a class="btn sub" href="{back}">{t("s078")}</a> <button class=ok>{t("s079")}</button></div></div></form>')
        self.send(200, page(f"{t('s071')} #{num}", h, repo, wide=True))

    def resolve_post(self, repo, num, it, f):
        src, dst = it["src"], it["dst"]
        s, d = resolve(repo, src), resolve(repo, dst)
        if it["state"] != "open" or not s or not d:
            return self.redirect(f"/r/{repo}/i/{num}")
        if f.get("base") != d or f.get("head") != s:
            return self.resolve_page(repo, num, it, t('s120'))
        try:
            n = int(f.get("n", "0"))
            choices = [(f.get(f"path{i}", ""), f.get(f"mode{i}", "edit"), f.get(f"text{i}", "")) for i in range(n)]
            if any(m not in ("edit", "ours", "theirs", "delete") for _, m, _ in choices):
                raise RuntimeError(t('s169'))
            sha = resolve_conflicts(repo, src, dst, d, s, choices, self.me["name"])
        except RuntimeError as ex:
            return self.resolve_page(repo, num, it, str(ex))
        with db() as c:
            c.execute("INSERT INTO comment(repo,num,body,author,created) VALUES(?,?,?,?,?)",
                      (repo, num, f"{t('s236')} ({sha[:10]}): "
                       + ", ".join(p for p, _, _ in choices), self.me["name"], now()))
        self.redirect(f"/r/{repo}/i/{num}?msg=" + urllib.parse.quote(t('s170') + ' Merge ' + t('s171')))

    def item_action(self, repo, num, f, _):
        user = self.user_from(f)
        act = f.get("action", "comment")
        with db() as c:
            it = c.execute("SELECT * FROM item WHERE repo=? AND num=?", (repo, num)).fetchone()
            if not it:
                return self.send(404, page("404", "<h2>Not Found</h2>", repo))
            if act in ("approve", "change") and it["kind"] == "pr" and it["state"] == "open":
                c.execute("INSERT INTO comment(repo,num,body,author,created,kind) VALUES(?,?,?,?,?,?)",
                          (repo, num, f.get("body", "").strip(), user, now(), act))
            elif f.get("body", "").strip():
                c.execute("INSERT INTO comment(repo,num,body,author,created) VALUES(?,?,?,?,?)",
                          (repo, num, f["body"], user, now()))
            if act == "close" and it["state"] == "open":
                c.execute("UPDATE item SET state='closed' WHERE repo=? AND num=?", (repo, num))
            elif act == "reopen" and it["state"] == "closed":
                c.execute("UPDATE item SET state='open' WHERE repo=? AND num=?", (repo, num))
            elif act == "merge" and it["kind"] == "pr" and it["state"] == "open":
                msg = f"Merge pull request #{num} from {it['src']}\n\n{it['title']}"
                sha = do_merge(repo, it["src"], it["dst"], msg, user)
                c.execute("UPDATE item SET state='merged', merge_sha=? WHERE repo=? AND num=?", (sha, repo, num))
                c.execute("INSERT INTO comment(repo,num,body,author,created) VALUES(?,?,?,?,?)",
                          (repo, num, f"merged {it['src']} into {it['dst']} ({sha[:10]})", user, now()))
        self.redirect(f"/r/{repo}/i/{num}", user)

    # --- ログイン
    @staticmethod
    def safe_next(n):
        return n if n.startswith("/") and not n.startswith("//") and not n.startswith("/login") else "/"

    def login_page(self, q, err="", name=""):
        nxt = self.safe_next(q.get("next", "/"))
        msg = f'<div class=msg>{E(q["msg"])}</div>' if q.get("msg") else ""
        if err:
            msg = f'<div class="msg err">{E(err)}</div>'
        self.send(401 if err else 200, page(t('s121'), f"""<div class=login><h2>minigit {t('s172')}</h2>{msg}
<div class=box><form method=post action="/login"><input type=hidden name=next value="{E(nxt)}">
<label>{t('s127')}<input type=text name=name value="{E(name)}" autocomplete=username required autofocus></label>
<label>{t('s027')}<input type=password name=pw autocomplete=current-password required></label>
<button>{t('s121')}</button></form></div></div>"""))

    def login_post(self, f):
        name, pw = f.get("name", "").strip(), f.get("pw", "")
        info = authenticate(name, pw) if name and pw else None
        if not info:
            import time
            time.sleep(0.5)
            sys.stderr.write(f"login failed: user={name!r} from {self.client_address[0]}\n")
            return self.login_page({"next": f.get("next", "/")}, t('s122'), name)
        self.redirect(self.safe_next(f.get("next", "/")), cookie=create_session(info["name"]))

    # --- CSRF: 画面からの POST は同じホストからのみ
    def same_origin(self):
        src = self.headers.get("Origin") or self.headers.get("Referer")
        if not src or src == "null":
            return True  # ブラウザ以外 (curl 等)
        return urllib.parse.urlsplit(src).netloc == self.headers.get("Host", "")

    # --- 管理画面
    def admin_page(self, q):
        with db() as c:
            rows = c.execute("SELECT * FROM users ORDER BY name").fetchall()
        msg = f'<div class=msg>{E(q["msg"])}</div>' if q.get("msg") else ""
        trs = ""
        for r in rows:
            n = E(r["name"])
            me = r["name"] == self.me["name"]
            trs += f"""<tr><td><b>{n}</b>{' (' + t('s211') + ')' if me else ''}</td>
<td>{t('s087') if r["admin"] else t('s212')}</td><td>{t('s213') if r["active"] else '<span class=warn>' + t('s214') + '</span>'}</td>
<td><small>{E(r["created"] or "")}</small></td><td>
<form method=post><input type=hidden name=name value="{n}">
<input type=password name=pw placeholder="{t('s123')}"><button name=action value=passwd>PW{t('s124')}</button>
<button name=action value=toggle_admin>{t('s215') if r["admin"] else t('s216')}</button>
<button name=action value=toggle_active>{t('s217') if r["active"] else t('s218')}</button>
{'' if me else '<button class=danger name=action value=delete onclick="return confirm(&quot;' + t('s219') + '&quot;)">' + t('s017') + '</button>'}
</form></td></tr>"""
        self.send(200, page(t('s096'), f"""<h2>{t('s096')}</h2>{msg}
<table><tr><th align=left>{t('s026')}</th><th align=left>{t('s173')}</th><th align=left>{t('s174')}</th><th align=left>{t('s175')}</th><th></th></tr>{trs}</table>
<div class=box><form method=post><b>{t('s176')}</b>
<input type=text name=name placeholder="{t('s127')} ({t('s139')} . _ @ -)" required>
<input type=password name=pw placeholder="{t('s027')} (4{t('s177')})" required>
<label><input type=checkbox name=admin value=1> {t('s087')}</label>
<button name=action value=add>{t('s015')}</button></form></div>"""))

    def admin_post(self, f):
        act, name, pw = f.get("action", ""), f.get("name", "").strip(), f.get("pw", "")
        msg = ""
        with db() as c:
            r = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
            if act == "add":
                if not USER_RE.match(name):
                    msg = t('s178') + ' (' + t('s139') + ' . _ @ -' + t('s179') + '64' + t('s180') + ')'
                elif r:
                    msg = f"{name} {t('s237')}"
                elif len(pw) < 4:
                    msg = t('s220') + '4' + t('s221')
                else:
                    c.execute("INSERT INTO users(name,pw,admin,active,created) VALUES(?,?,?,1,?)",
                              (name, hash_pw(pw), 1 if f.get("admin") else 0, now()))
                    msg = f"{name} {t('s241')}"
            elif not r:
                msg = t('s181')
            elif act == "passwd":
                if len(pw) < 4:
                    msg = t('s220') + '4' + t('s221')
                else:
                    c.execute("UPDATE users SET pw=? WHERE name=?", (hash_pw(pw), name))
                    c.execute("DELETE FROM sessions WHERE name=?", (name,))
                    msg = f"{name} {t('s242')} ({t('s243')})"
            elif act == "toggle_admin":
                if r["admin"] and r["active"] and active_admins(c) <= 1:
                    msg = t('s244')
                else:
                    c.execute("UPDATE users SET admin=? WHERE name=?", (0 if r["admin"] else 1, name))
                    msg = f"{name} {t('s072')}{t('s252') if r['admin'] else t('s087')}{t('s248')}"
            elif act == "toggle_active":
                if name == self.me["name"] and r["active"]:
                    msg = t('s249')
                elif r["admin"] and r["active"] and active_admins(c) <= 1:
                    msg = t('s250')
                else:
                    c.execute("UPDATE users SET active=? WHERE name=?", (0 if r["active"] else 1, name))
                    c.execute("DELETE FROM sessions WHERE name=?", (name,))
                    msg = f"{name} {t('s072')}{t('s214') if r['active'] else t('s213')}{t('s248')}"
            elif act == "delete":
                if name == self.me["name"]:
                    msg = t('s251')
                elif r["admin"] and r["active"] and active_admins(c) <= 1:
                    msg = t('s253')
                else:
                    c.execute("DELETE FROM users WHERE name=?", (name,))
                    c.execute("DELETE FROM sessions WHERE name=?", (name,))
                    msg = f"{name} {t('s106')}"
        users_changed()
        self.redirect("/admin?msg=" + urllib.parse.quote(msg))

    # --- 自分のパスワード変更
    def account_page(self, q):
        msg = f'<div class=msg>{E(q["msg"])}</div>' if q.get("msg") else ""
        host = self.headers.get("Host", "localhost")
        inp = ""
        self.send(200, page(t('s095'), f"""<h2>{t('s095')}: {E(self.me["name"])}</h2>{msg}
<div class=box><form method=post><b>{t('s182')}</b>
<input type=password name=cur placeholder="{t('s183')}" required {inp}>
<input type=password name=new1 placeholder="{t('s184')} (4{t('s177')})" required {inp}>
<input type=password name=new2 placeholder="{t('s184')} ({t('s185')})" required {inp}>
<button>{t('s016')}</button></form></div>
<p>git {t('s186')}
{t('s187')} → Windows {t('s188')} <code>git:http://{E(host)}</code> {t('s189')}</p>"""))

    def account_post(self, f):
        name = self.me["name"]
        with db() as c:
            r = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
            if not r or not check_pw(f.get("cur", ""), r["pw"]):
                msg = t('s125')
            elif f.get("new1", "") != f.get("new2", ""):
                msg = t('s190')
            elif len(f.get("new1", "")) < 4:
                msg = t('s220') + '4' + t('s221')
            else:
                c.execute("UPDATE users SET pw=? WHERE name=?", (hash_pw(f["new1"]), name))
                c.execute("DELETE FROM sessions WHERE name=? AND token<>?", (name, _tok_hash(self.cookie(SESSION_COOKIE))))
                msg = t('s222') + ' (' + t('s223') + ')'
        users_changed()
        self.redirect("/account?msg=" + urllib.parse.quote(msg))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class DualStackServer(ThreadingHTTPServer):
    """IPv6 + IPv4 両方で待受 (Windows の localhost -> ::1 試行による約2秒の遅延を回避)"""
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class V4Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(port):
    host = os.environ.get("MINIGIT_HOST")
    if host:
        cls = DualStackServer if ":" in host else V4Server
        return cls((host, port), H)
    try:
        return DualStackServer(("::", port), H)
    except OSError:
        return V4Server(("0.0.0.0", port), H)


def disable_quickedit():
    """Windows コンソールの簡易編集モードを無効化。
    ウィンドウをクリックして「選択」状態になるとログ出力がブロックされ、全リクエストが止まるため。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)  # ~QUICK_EDIT | EXTENDED_FLAGS
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 6 and sys.argv[1] == "hook-update":
        sys.exit(hook_update(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))
    if len(sys.argv) >= 2 and sys.argv[1] == "passwd":
        if len(sys.argv) < 3 or not USER_RE.match(sys.argv[2]):
            sys.exit(t('s126') + ': python minigit.py passwd <' + t('s127') + '> [<' + t('s123') + '>]')
        os.makedirs(ROOT, exist_ok=True)
        new = sys.argv[3] if len(sys.argv) >= 4 else secrets.token_urlsafe(9)
        print(set_password(sys.argv[2], new))
        if len(sys.argv) < 4:
            print(f"{t('s123')}: {new}")
        sys.exit(0)
    disable_quickedit()
    if not shutil.which(GIT) and not os.path.isfile(GIT):
        sys.exit(f"git {t('s128')}: {GIT}\n{t('s129')} MINIGIT_GIT {t('s011')} git.exe {t('s130')}")
    try:
        ver = run([GIT, "--version"]).stdout.decode(errors="replace").strip()
    except Exception as ex:
        ver = str(ex)
    if not ver.startswith("git version"):
        sys.exit(f"{GIT} {t('s010')} git {t('s131')} (git --version {t('s132')}: {ver!r})\n"
                 f"Git for Windows {t('s133')} <{t('s134')}>\\cmd\\git.exe {t('s072')} MINIGIT_GIT {t('s135')}")
    print(f"git: {GIT} ({ver})")
    os.makedirs(ROOT, exist_ok=True)
    db().close()
    with db() as c:
        for r in c.execute("SELECT DISTINCT repo FROM protect"):
            if os.path.isdir(repo_path(r["repo"])):
                set_update_hook(r["repo"])
    first = init_users()
    if first:
        print(first)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    srv = make_server(port)
    print(f"minigit: http://localhost:{port}/  listen={srv.server_address[0]}  root={ROOT}  "
          f"users={db().execute('SELECT COUNT(*) FROM users WHERE active=1').fetchone()[0]}")
    srv.serve_forever()
