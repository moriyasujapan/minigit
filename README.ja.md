# minigit

[English](README.md) | [日本語](README.ja.md) | [中文](README.zh.md)

閉域網向けの最小 Git ホスティング。Python 標準ライブラリと git CLI だけで動き、
追加パッケージも DB サーバーも不要。

![ホーム](docs/screenshots/home_ja.png)

## 機能

- HTTP での clone / push。ユーザーアカウント (SQLite、PBKDF2-SHA256)
- Issue と Pull Request。左右分割 / 統合の差分表示、単語単位の強調、
  ブラウザ上でのコンフリクト解消 (git 2.38 以上)
- ブランチの作成 / 削除、保護ブランチ、Web 編集とコミット
- 構文ハイライト (py/java/js/c/sh/sql/html/css/ini) を自作トークナイザで。JS 不要
- ライト / ダークテーマ
- 英語・日本語・中国語の UI。[言語](#言語)を参照
- 単一ファイルの自己展開配布 (`pack.py` + `split.sh`)。[閉域への持ち込み](#閉域への持ち込み)を参照

## 必要環境

- Python 3.8 以上
- git 2.x (ブラウザでのコンフリクト解消は git 2.38 以上)

## 起動

```sh
./run.sh            # http://localhost:8080/
./run.sh 9000       # ポート指定
```

初回起動時に `admin` ユーザーがランダムパスワードで作成され、コンソールに表示されます。
リポジトリと DB は `./repos` に作られます。

環境変数:

| 変数 | 内容 | 既定 |
|---|---|---|
| `MINIGIT_ROOT` | リポジトリと DB の置き場 | `./repos` |
| `MINIGIT_USERS` | 初回起動時に取り込むユーザー `alice:pw1,bob:pw2` (先頭が管理者) | なし |
| `MINIGIT_GIT` | git のパス | 自動検出 |
| `MINIGIT_HOST` | 待ち受けアドレス | IPv4 / IPv6 両方 |
| `MINIGIT_LANG` | UI 言語のフォールバック (`en` / `ja` / `zh`) | `en` |
| `MINIGIT_DEBUG` | `1` で実行した git コマンドを表示 | なし |

パスワードを忘れた場合: `python3 minigit.py passwd ユーザー名 新パスワード`

## git の使い方

```sh
git clone http://ユーザー名:パスワード@localhost:8080/リポジトリ名.git
```

## 言語

UI は英語・日本語・中国語 (簡体字) に対応しています。文言は
`minigit.py` と同じ dir の `minigit_en.json` / `minigit_ja.json` /
`minigit_zh.json` に格納されています。言語は次の順で決まります:

1. URL の `?lang=en|ja|zh` (Cookie にも保存)
2. 以前に保存された `mg_lang` Cookie
3. ブラウザの `Accept-Language` ヘッダー
4. `MINIGIT_LANG`、未設定なら `en`

全ページのヘッダーに言語切替があります。欠けたキーは英語 → 日本語の順に
フォールバックします。言語の追加は JSON をコピーして値を翻訳し、再起動するだけです。
キーの値は `{...}` を含まない平文なので、コードに触れずに JSON だけ翻訳できます。

`extract.py` は minigit.py から文言を JSON に切り出した codemod です。
開発用ツールで、サーバーの動作には不要です。

![ファイル表示](docs/screenshots/file_en.png)

## 閉域への持ち込み

Python と cmd.exe だけの Windows に、PowerShell なしで持ち込めます。
`pack.py` は `minigit.py` と 3 つの言語ファイルを 1 つの ASCII 自己展開ファイル
`minigit_packed.py` にまとめ、`split.sh` は 20 KB 以下に分割します。

```
[Mac/Linux]  python3 pack.py  ->  ./split.sh minigit_packed.py 19
[転送]       minigit_packed.py.part.*, minigit_packed.py.sha256, join.cmd, unpack.py
[Windows]    join.cmd minigit_packed.py  ->  python minigit_packed.py 8080
                                        ->  python unpack.py   (全ソースに復元)
```

| ファイル | 内容 |
|---|---|
| `minigit.py` | 本体 |
| `minigit_<lang>.json` | 言語別の UI 文言 |
| `run.sh` | Mac / Linux 用起動スクリプト |
| `pack.py` / `unpack.py` | 自己展開バンドル (bz2 + base85) の作成 / SHA256 照合付き復元 |
| `split.sh` / `join.sh` | 分割 / 結合 (Mac / Linux) |
| `split.cmd` / `join.cmd` | 分割 / 結合 (Windows cmd のみ) |

## 構成

サーバーに必要なものは `minigit.py` と JSON 3 ファイルだけです。
`schema.sql` はデモリポジトリ用のサンプルにすぎません。

## ライセンス

MIT
