# Zendesk中継APIの一括テスト

`test_all_apis.py`は送信元IPが許可済みの連携ホストで実行する
Python 3.8以上・追加パッケージ不要のテストスクリプトです。
Bearerトークンは指定ファイルから読み、コマンド引数や出力には展開しません。

## 実行

`test_all_apis.py`を実行ホストへコピーし、トークンファイルを用意します。

```bash
chmod 600 /path/to/zendesk_fugaku_relay_token
python3 test_all_apis.py \
  --token-file /path/to/zendesk_fugaku_relay_token \
  --base-url https://relay.example.com/zendesk_fugaku \
  --upstream-url https://your-subdomain.zendesk.com
```

接続先は必須です。`ZENDESK_RELAY_TEST_BASE_URL`と`ZENDESK_RELAY_TEST_UPSTREAM_URL`を非公開の環境設定から指定することもできます。後者はページネーション元の許可originです。cleanup-onlyは保存済み接続先を使い、異なるoriginを自動追跡しません。

全APIを使うため、実行するとZendeskに専用のend-user 2件・組織2件を作成し、
それらに組織所属を設定、notes・identityを更新して、最後に削除します。
テストIDは`zrelaytest-日付-ランダム文字列`で毎回生成します。既存のIDやメール
アドレスを更新対象として指定するオプションはありません。書き込み前と削除前に
external_id・nameの識別子を照合し、ユーザーはend-userであることも確認します。
ユーザーには`@example.com`のテスト用アドレスを使い、確認メールを抑制します。
チケット・agent・admin・既存グループは作成/更新しません。

必要なAPIすべての作成/削除権限を持つZendesk側のOAuth認証が必要です。
閲覧専用の権限やテナント制約がある場合はFAILとして報告します。

## 確認する経路

| メソッド | `/api/v2/`以下の経路 |
|---|---|
| GET | `users.json`（agent/admin、end-userの2パターン）、`organizations.json`、`organization_memberships.json`、`groups.json`、`group_memberships.json` |
| GET | `organizations/search.json`、`organizations/show_many.json`、`users/search.json`、`users/show_many.json`、`job_statuses/{id}.json` |
| POST | `organizations/create_many.json`、`users.json`、`users/create_many.json`、`organization_memberships.json`、`organization_memberships/create_many.json` |
| PUT | `organizations/update_many.json`、`users/{id}.json`、`users/update_many.json`、`users/{user_id}/identities/{identity_id}.json` |
| DELETE | `organizations/destroy_many.json`、`users/destroy_many.json` |

一覧は1ページ1件、次ページがあれば追加1ページだけ確認します。全データの
ダウンロードはしません。次ページURLは期待するZendeskホストと同じ経路だけを
受け付け、トークンを付けた接続先は常に中継URLに固定します。HTTP redirectも
追従しません。identity IDは`users/show_many.json?include=identities`で取得します。
検索はAPIの疎通・配列の形式を確認します。検索インデックスの反映待ちはしません。
作成・更新したレコードの検証はexternal_idsによるshow_manyを使用します。

## 結果と終了コード

各テストにPASS/FAIL/SKIPを表示し、HTTPステータスも表示します。
トークン、既存ユーザー/組織の本文、上流エラー本文は出力・保存しません。
既存データは一覧の1〜2件をメモリ内で扱うだけです。

実行ディレクトリに、実行IDを名前とする権限700のディレクトリを作ります。
`--output-dir 新規ディレクトリ名`で変更できます。既存ディレクトリは上書きしません。

- `report.json`：テスト結果・HTTPステータス・cleanup結果（権限600）
- `state.json`：実行ID・作成したテストデータのID・未完了job・不明な作成結果（権限600）
- 終了コード0：全テストと削除確認が成功
- 終了コード1：テスト失敗・前提不足でSKIP・削除未完了・中断

ユーザー削除は指定の`users/destroy_many`による通常の削除です。
Zendeskの完全削除APIは使いません。Zendeskの削除済み領域や監査記録まで消すことを
意味しません。

## 途中失敗・中断と後片付け

通常例外・Ctrl-C・SIGTERMでもfinallyで後片付けを試みます。
非同期jobの完了と各itemの失敗を検査し、削除後もshow_manyで残存を確認します。
強制kill、停電、ネットワーク断では削除を保証できません。
未完了jobや作成結果不明のままでは、cleanup成功として扱いません。
作成・更新リクエストは重複登録を避けるため自動再送しません。

削除未完了時は、同じトークンファイルと表示された状態ファイルを使います。
このモードは新規データを作成せず、保存済みjobとその実行IDのデータだけを確認・削除します。

```bash
python3 test_all_apis.py \
  --token-file /path/to/zendesk_fugaku_relay_token \
  --cleanup-only ./zrelaytest-YYYYMMDD-xxxxxxxxxxxx/state.json
```

`--job-timeout 300`でjob待ち時間（既定180秒/1job）を延長できます。
不明な作成結果が解消しない場合は、状態ファイルの実行IDでZendesk管理者に
確認を依頼してください。状態ファイルを別実行のものへ変更しないでください。

## 通信なしの検証

このディレクトリの親`apps/zendesk_fugaku`から実行します。

```bash
python3 -m unittest discover -s tools -p test_test_all_apis.py -v
```

正常系全経路、部分作成、作成応答欠落、未完了job、削除失敗後の再実行、
所有マーカー不一致、ページングURL制限などをmock APIで検証します。
実際のZendesk・中継サーバには接続しません。

## 仕様の参照先

- [Users API（作成時のメール抑制・一括削除）](https://developer.zendesk.com/api-reference/ticketing/users/users/)
- [Organizations API](https://developer.zendesk.com/api-reference/ticketing/organizations/organizations/)
- [Organization Memberships API](https://developer.zendesk.com/api-reference/ticketing/organizations/organization_memberships/)
- [Job Statuses API（jobと各itemの結果）](https://developer.zendesk.com/api-reference/ticketing/ticket-management/job_statuses/)
- [User Identities API](https://developer.zendesk.com/api-reference/ticketing/users/user_identities/)
- [Sideloading（Usersのidentities）](https://developer.zendesk.com/documentation/api-basics/working-with-data/side_loading/)
