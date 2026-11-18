# Open Agent Memory

**日常の AI エージェントに、役立つメモリを。**

[English](README.md) | [中文](README_CN.md) | [日本語](README_JA.md)

[はじめに](#はじめに) · [性能](#パフォーマンスと精度) · [エージェント連携](#エージェントを接続する) · [ドキュメント](docs/indexing-retrieval.md) · [リリース](https://github.com/rocke2020/open-agent-memory/releases)

Open Agent Memory（OAM）は、AI エージェント向けの完全にオープンソースのメモリエンジンです。会話をまたいで有用な事実、好み、経験を記憶し、エージェントが必要とするときに、関連する証拠を出典付きで取り出します。

会話と文書を自分の PostgreSQL データベースに保存し、モデルプロバイダーを選び、エージェントが何を記憶しているか確認できます。OAM がメモリを提供し、エージェントが回答を作成します。

## パフォーマンスと精度

**低コストの Flash モデルを使い、対応する Hindsight の比較結果を上回る記録スコアと、LongMemEval S で 73.63% 小さい保存済みコンテキストを実現しました。**

| ベンチマーク | OAM | Hindsight の比較結果 | 回答コンテキスト |
| --- | --- | --- | --- |
| **LongMemEval S**、全 500 問 | **95.2%（476/500）** | 94.6%（473/500） | 保存済みコンテキストは平均 **11,373.58** トークン、比較対象は 43,124.55；**73.63% 削減** |
| **BEAM-10M**、完全な履歴 10 件に対する 200 問 | **61.56%** 平均ルーブリックスコア | 52.16%、保存済みコンテキストの再評価 | OAM のコンテキストは 17.43% 多い |

**LME-500：Hindsight との公平な比較のため、同じ質問・参照回答と、バイト単位で同一の回答・判定プロンプトテンプレートを使用。** OAM は Hindsight の公開 AMB 参照リビジョンの回答・カテゴリー別判定プロンプトテンプレートをバイト単位でそのまま再利用し、回答指示と判定基準を揃えています。[プロンプトの出典と評価設定](docs/benchmarks.md#1-latest-longmemeval-s-476500-at-18k)を参照してください。

最新の LongMemEval の結果では、回答と判定に **MiniMax M3.1 Flash** を使用し、コンテキストの上限は 18,000 トークンです。同じコンテキストを使った以前の LongMemEval 実行では、**DeepSeek Flash** の結果も記録しています。BEAM-10M は回答と判定に **DeepSeek Flash** を使用し、レポートには記録時の high/max 回答設定を保持しています。いずれも低コストの Flash モデルによる評価パイプラインであり、高価な最上位モデルを回答用に使う必要はありません。

比較対象とは回答・判定モデルとメモリ処理が異なります。コンテキストのサイズは保存された整形済みテキストのトークン数であり、プロバイダーの課金トークン数ではありません。現在の LME の平均値には 497 件のコンテキストを使用し、正解データに疑義がある 3 問も 500 問のスコアでは不正解として数えています。評価対象は L1 メモリであり、Insights、User Profile、文書 Resources は採点していません。

回答・判定モデルを再実行すると、スコアが変わることがあります。[同じ入力を使った MiniMax の実例](docs/benchmarks.md#answer-and-judge-variability)では、一方の回答は正解、もう一方は不正解でした。これは観測された変動の例であり、その発生頻度を示すものではありません。

[結果・モデル料金・評価方法](docs/benchmarks.md) · [LME の質問ごとのエビデンス](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/lme500-20261117) · [BEAM の質問ごとのエビデンス](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/beam10m)

## OAM を選ぶ理由

- **セッションをまたいで記憶。** 役立つ好み、決定、経験を保存し、新しい会話でもエージェントが利用できます。
- **証拠を確認。** 検索したメモリから元のメッセージを辿れます。Memory Inspector でメモリの閲覧や削除ができます。
- **会話と文書をまとめて検索。** 学習メモリとともに、関連するテキスト、HTML、PDF、画像の Resources を検索できます。文書は学習された個人メモリとは分けて保持します。
- **役立つ個人プロフィールを作成。** 対象となるメモリと構造化された Profile Data から、保存済みの User Profile を明示的に更新できます。
- **データとモデルを自分で管理。** PostgreSQL/pgvector でセルフホストし、独自のエンドポイントを設定し、検索コンテキストの予算を制御できます。

## はじめに

**Docker と Compose**、**Git**、テキストと画像の入力をサポートする生成モデルのエンドポイント、OpenAI 互換の embedding エンドポイントが必要です。OAM は既定のモデルエンドポイントや認証情報を提供しません。モデルの事前チェックとインデックス作成では、設定したプロバイダーを呼び出すため、料金が発生する場合があります。

### 1. 設定を準備する

```bash
git clone https://github.com/rocke2020/open-agent-memory.git
cd open-agent-memory
cp deploy/.env.example deploy/.env
```

`deploy/.env` を編集します。

- `VLM_BASE_URL`、`VLM_API_KEY`、`VLM_MODEL` と `OAM_EMBEDDING_URL`、`OAM_EMBEDDING_KEY`、`OAM_EMBEDDING_MODEL` にプロバイダーの設定を指定します。
- 各データベースのパスワードと `OAM_ROOT_API_KEY` に、それぞれ異なるランダムな値を設定します。各値は `openssl rand -hex 24` で生成できます。
- embedding の既定の次元数は **1024** です。エンドポイントが別のサポート対象の次元数を返す場合は、**初期化前に** `deploy/oam.yaml` の `index.embedding_dimensions` を設定してください。[モデル設定](docs/operations.md#1-configuration)を参照してください。

### 2. OAM を起動する

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait postgres
docker compose --env-file deploy/.env -f deploy/compose.yaml run migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres psql -U postgres -d oam -f /bootstrap/runtime-roles.sql
docker compose --env-file deploy/.env -f deploy/compose.yaml run api preflight --json
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait api worker
```

コマンドが失敗した場合は、そこで止めてエラーを解決してから続けてください。Compose ファイルは Linux AMD64 または ARM64 向けの **v0.1.0** イメージを選択します。ポート、ホスト上のモデルサーバー、ソースからのビルド、レプリカ構成は[デプロイガイド](deploy/README.md)を参照してください。

### 3. 最初のメモリを保存して検索する

**Memory Inspector：<http://127.0.0.1:17333/ui/>** を開きます。[個人コレクションの作成](docs/start-here.md#2-create-your-personal-collection)、[好みの保存と検索](docs/start-here.md#3-store-and-recall-a-preference)の順に進めてください。ユーザーキーを作成し、簡単な好みを保存し、インデックス作成を待ってから、出典付きで取り出します。

最初の成功の目印は、検索した好みの出典から元のメッセージを確認できることです。その後でエージェントを接続し、新しいセッションでその好みを使うよう依頼してください。

**[最初のメモリまでの完全ガイド →](docs/start-here.md)**

## エージェントを接続する

先にサービスを起動し、ユーザーキーを作成してください。クライアントパッケージは OAM に接続するもので、サーバーを起動するものではありません。

| エージェント・アプリケーション | 接続方法 |
| --- | --- |
| Claude Code または Cursor | [MCP サーバーとクライアント設定](integrations/mcp/README.md#client-configuration)、Node.js 22+ |
| OpenClaw | [プラグインのインストール](integrations/openclaw/README.md#npm-installation)、自動検索と対象コンテンツの自動保存に対応 |
| DeepSeek Harness（DSH） | [DSH プラグイン](integrations/dsh/README.md) |
| 独自アプリケーション | [コピーして使える HTTP の例](docs/quickstart.md)と [API リファレンス](docs/api.md) |

## エージェントの記憶を確認する

![検索した証拠と元の出典を表示する Memory Inspector](docs/assets/inspect-memory.png)

Inspector では、保存した出典の閲覧、検索したメモリと出典の確認、Resources の管理、User Profile の更新ができます。このスクリーンショットは合成デモデータを使用しています。[Inspector ガイド](docs/memory-web-ui.md)。

## メモリの仕組み

OAM は元の会話（**L0**）を保持し、出典に裏付けられた事実（**L1**）を抽出し、関連する事実から根拠のある Insights（**L2**）を作成します。明示的に更新する User Profile（**L3**）は、エージェントの振る舞いの個人化に役立ちます。テキスト、HTML、PDF、画像の **Resources** は独立した証拠として保持し、学習メモリとともに検索できます。

検索は **fast** と **normal** モードをサポートし、最終回答は生成せず、出典付きコンテキストを返します。コンテキスト上限の既定値は、それぞれ 18,000 と 24,000 トークンです。証拠の使い方、最終回答、入力全体の予算はエージェントが管理します。

[メモリと検索の設計](docs/indexing-retrieval.md) · [アーキテクチャ](docs/architecture.md) · [設定](configs/README.md)

## v0.1.0

このリリースは、上記の検索品質のマイルストーンです。低コストの Flash モデルと小さな LME コンテキストで、より高いベンチマークスコアを記録しました。出典となる証拠の選択とコンテキスト形式を改善し、normal の既定コンテキスト上限を 24,000 トークンに下げ、任意で有効化する fast スコアリングと実験的な出典本文ポリシーを追加しています。v0.0.3 からのアップグレードでは既存のインデックスを再利用でき、新たなマイグレーションや再インデックスは不要です。

検証と公開の状況は[リリースノートとアップグレードガイド](docs/releases/0.1.0.md)を参照してください。OpenClaw は独立したバージョン 0.0.2、MCP と DSH は 0.0.1 のままです。

## ドキュメントと開発

| 目的 | ガイド |
| --- | --- |
| OAM のデプロイ・アップグレード | [Docker Compose](deploy/README.md) |
| HTTP での保存と検索 | [クイックスタート](docs/quickstart.md) · [API リファレンス](docs/api.md) |
| プロバイダー設定とサービス運用 | [運用](docs/operations.md) |
| 検証済みの動作と制約の確認 | [受け入れ要件](docs/acceptance.md) · [リリースノート](docs/releases/0.1.0.md) |
| ビルド・テスト・開発への参加 | [貢献ガイド](CONTRIBUTING.md) · [リリース手順](docs/releasing.md) |

今後は、導入体験の改善、インデックス作成と回答コンテキストのトークン削減、Resources の共有所有権に取り組む予定です。Python SDK、ホスティングサービス、データベースの高可用性、OIDC/LDAP 対応は今後の対象です。設定や検索に関するフィードバックは [Discussions](https://github.com/rocke2020/open-agent-memory/discussions/1) で共有してください。

## 謝辞

OAM は、[Hindsight](https://github.com/vectorize-io/hindsight)、[OpenViking](https://github.com/volcengine/OpenViking)、[Mem0](https://github.com/mem0ai/mem0)、[MemOS](https://github.com/MemTensor/MemOS)、[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)、[gbrain](https://github.com/garrytan/gbrain) のオープンソースの取り組みを参考にしています。[LongMemEval](https://github.com/xiaowu0162/LongMemEval) と [BEAM](https://github.com/mohammadtavakoli78/BEAM) の著者および貢献者の皆様にも感謝します。[謝辞の全文](ACKNOWLEDGMENTS.md)と[サードパーティに関する表示](THIRD_PARTY.md)を参照してください。

## ライセンス

[Apache License 2.0](LICENSE)。
