# QB Sync: QuickBooks Desktop → n8n / Postgres

QB Sync keeps a **read-only** copy of a QuickBooks Desktop company file up to date with incremental syncs, and **pushes** every change out:

- to **n8n** (or any webhook): signed JSON events over outbound HTTPS, so no ports are opened on the machine
- to **Postgres / Supabase**: upserts into a `qb_records` table

It runs with **standard Windows user rights**, and a standard user can install it without admin rights.

```
 QuickBooks Database Server Manager            Workstation (Windows, standard user)
 ┌──────────────────────────────┐   SMB    ┌──────────────────────────────────────────────┐
 │ \\QBSERVER\QBData\Co.QBW      │◄────────►│ QuickBooks Desktop (installed, not running)  │
 │ (QBDBMgrN service)           │          │   ▲ qbXML queries via QBXMLRP2 (COM)         │
 └──────────────────────────────┘          │ qbsync run  (scheduled task, no window)      │
                                           │   └─ SQLite store + change cursor            │──► HTTPS POST ──► n8n webhook
                                           └──────────────────────────────────────────────┘──► Postgres / Supabase
                                                        nothing listens; outbound only
```

## Why it goes through the QuickBooks SDK

Database Server Manager hosts the company file as an encrypted SQL Anywhere database. Intuit doesn't provide credentials for it, and connecting to it directly isn't supported. Doing so risks corrupting the file. The supported route is the **QuickBooks SDK (qbXML)**. The SDK calls a QuickBooks Desktop install on the sync machine, and that install opens the file over the network the same way a workstation does. QB Sync sends **only `*Query` requests**, so it never writes to QuickBooks.

## Requirements

- A Windows machine with **QuickBooks Desktop installed**, matching the edition and year that hosts the file (e.g. Enterprise 24). Any workstation that can open the file will do. QuickBooks doesn't need to be running.
- A Windows account that can open QuickBooks and reach the company file share. It **does not need admin rights**.
- Outbound HTTPS to your n8n instance, or outbound access to Postgres if you use that.
- A **QuickBooks Admin** login for the one-time authorization below. This is the Admin user inside QuickBooks, not a Windows administrator.

## Install

Run `QBSync-Setup-<version>.exe`. It bundles its own 32-bit Python, so nothing else needs to be installed. The install mode depends on who runs it:

| Run by | Mode | Program | Config, data and logs | Background task runs as |
|---|---|---|---|---|
| **Standard user** (no UAC prompt), or anyone with `/CURRENTUSER` | per-user | `%LOCALAPPDATA%\Programs\QB Sync` | `%LOCALAPPDATA%\QBSync` | that user |
| **Administrator** (UAC prompt), or SYSTEM via an RMM | machine-wide | `C:\Program Files (x86)\QB Sync` | `C:\ProgramData\QBSync`, restricted to Administrators, SYSTEM and the run-as account | the `/USER` account (default: the installing user) |

In both modes QB Sync itself runs with standard user rights. The scheduled task uses the lowest privilege level.

The wizard asks for:
- the company file (use the UNC path, e.g. `\\QBSERVER\QBData\Company.QBW`)
- the n8n webhook URL and secret (optional; you can add them later)
- whether to run in the background at the user's logon

It also adds Start Menu shortcuts: **Test QuickBooks Connection**, **Sync Now**, **Send Test Webhook**, **Sync Status**, **Edit Configuration** and **Open Data and Logs Folder**.

**Silent install:**

```bat
:: as the logged-in standard user (per-user, no admin needed)
QBSync-Setup-0.3.7.exe /S /COMPANYFILE="\\QBSERVER\QBData\Acme.QBW" ^
    /WEBHOOKURL="https://n8n.example.com/webhook/quickbooks" /WEBHOOKSECRET="<shared secret>"

:: from an RMM as SYSTEM (machine-wide; the task runs as the standard user ACME\jsmith)
QBSync-Setup-0.3.7.exe /S /COMPANYFILE="\\QBSERVER\QBData\Acme.QBW" /USER="ACME\jsmith" ^
    /WEBHOOKURL="https://n8n.example.com/webhook/quickbooks" /WEBHOOKSECRET="<shared secret>"
```

Other options: `/COMPANYID=acme`, `/NOTASK`, `/PASSWORD=...` (see *Background task* below) and `/D=<install dir>` (must be last).

**Upgrades:** run a newer installer in the same mode. The configuration and synced data are kept, and the background task is restarted.

**Uninstall:** use *Apps & features* or `Uninstall.exe /S` in the install folder. The data folder is kept unless you choose to delete it.

**Command line:** `qbsync.cmd <command>` in the install folder uses that install's config by default. The *QB Sync Command Prompt* shortcut opens a prompt there.

The installer isn't code-signed, so Windows SmartScreen will warn on first run. Sign it with your code-signing certificate (`signtool sign ...`) before rolling it out widely.

### From source (any Python 3.10+)

```powershell
py -m pip install .
qbsync -c C:\QBSync\config.yaml init --company-file "\\QBSERVER\QBData\Company.QBW" --webhook-url https://n8n.example.com/webhook/quickbooks
qbsync -c C:\QBSync\config.yaml service install
```

If you get *"Class not registered"* when connecting, use 32-bit Python. It matches the QuickBooks SDK components.

### Building the installer

On Linux, WSL or macOS with `python3`, `curl` and NSIS (`apt install nsis` / `brew install makensis`), run `installer/build.sh`. It downloads a checksum-verified 32-bit CPython build (python-build-standalone) and installs the pinned 32-bit Windows wheels from `installer/requirements-win32.txt`. The result is `dist/QBSync-Setup-<version>.exe`, plus a `.sha256` file.

## First-time authorization (one time, needs the QuickBooks Admin login)

1. On the sync machine, logged in to Windows as the account QB Sync will run as, open QuickBooks and log in to the company file as the QuickBooks **Admin** user.
2. Run **Test QuickBooks Connection** (`qbsync test-connection`). QuickBooks shows *"An application is requesting access…"*.
3. Choose **"Yes, always; allow access even if QuickBooks is not running"**, then select the QuickBooks user the app will log in as. A dedicated QuickBooks user with view access to everything you want synced is a good choice, because the sync can only see what that user can see.
4. Close QuickBooks and run the test again. It should print the company name with QuickBooks closed.

You can review or revoke access later in QuickBooks under **Edit → Preferences → Integrated Applications → Company Preferences**.

## Run it

```powershell
qbsync sync              # one incremental sync + push (first run pulls everything)
qbsync sync --full       # ignore watermarks and re-pull everything
qbsync run               # sync + push every interval_minutes (what the background task runs)
qbsync test-webhook      # send a test event to the webhook
qbsync status            # counts, watermarks, last errors, sink cursors
qbsync export --out C:\Temp\qb-export   # JSONL per entity + reports.json
```

Run the first full sync outside business hours on large files, because it can slow QuickBooks for other users. On the first sync QuickBooks can take several minutes to answer for big lists like invoices; the log shows `still waiting for QuickBooks…` every minute and `N so far, M remaining` per batch. Each record type is pushed to n8n as soon as it's pulled. Set `sync.history_start_date` (e.g. `'2023-01-01'`) to skip older transactions. Later runs only fetch what changed.

**Background task:** the installer sets this up. Otherwise run `qbsync service install`, which creates a `QBSync` scheduled task that runs `qbsync run` at the user's logon, with no window, and restarts on failure. Standard users can create this task for themselves; no admin rights are needed.
- To start at boot without anyone logged on, use `qbsync service install --password ...`. The account then needs the *Log on as a batch job* right. Standard users don't always have it, and granting it needs an admin. The logon mode avoids that and is also the most reliable mode for QuickBooks.
- Manage the task with `qbsync service start|stop|status|uninstall`.

**Multiple company files:** use one config file and database per company, and give each its own task, e.g. `qbsync -c acme.yaml service install --name QBSync-Acme`.

## What gets synced

| Lists | Transactions |
|---|---|
| Account, Customer (incl. jobs), Vendor, Employee, OtherName, Item (all item types), Class, StandardTerms, PaymentMethod, ShipMethod, SalesRep, SalesTaxCode, CustomerType, VendorType | Invoice, SalesReceipt, ReceivePayment, CreditMemo, Estimate, SalesOrder, Bill, BillPaymentCheck, BillPaymentCreditCard, Check, CreditCardCharge, CreditCardCredit, Deposit, JournalEntry, PurchaseOrder, ItemReceipt, VendorCredit, InventoryAdjustment, TimeTracking |

- Records include line items, linked transactions and custom fields (`DataExtRet`). Inactive list entries are included and flagged with `is_active`.
- **Incremental sync:** each entity is filtered by `TimeModified` since the last successful run and paged with qbXML iterators. Records that come back unchanged are detected by content hash and not pushed again.
- **Deletes:** picked up with `ListDeletedQuery` / `TxnDeletedQuery` and pushed as `deleted: true`.
- **Report snapshots** are refreshed every run: P&L (YTD, last month, by month), balance sheet, and customer/vendor balances. Configure them under `reports:`.
- **Error isolation:** if one entity fails (for example, SalesOrder isn't available in QuickBooks Pro), it's logged and the other entities still sync. Its watermark doesn't move, so the next run retries it. Add entities you don't have to `sync.exclude`.

Each record has the full QuickBooks object in `data`, plus extracted fields: `label` (name or ref #), `party` (customer/vendor/payee), `txn_date`, `amount`, `balance` (open/unpaid amount), `is_active`, `time_modified`, `deleted` and `change_seq`.

## Pushing to n8n (no open ports needed)

The webhook sink only makes **outbound HTTPS requests**, so it works from a workstation behind NAT or a firewall with nothing listening. If the workstation can browse to your n8n URL, it can push to it.

**n8n side:**
1. Add a **Webhook** node: method `POST`, path e.g. `quickbooks`.
2. Set **Authentication** to *Header Auth*, with a credential whose name is `X-QBSync-Token` and whose value is your shared secret.
3. Turn on **Respond: Immediately** so QB Sync isn't kept waiting while the workflow runs.
4. Activate the workflow and copy its **Production URL**.

**Workstation side:**
- Installer: enter the URL and secret in the wizard, or pass `/WEBHOOKURL=... /WEBHOOKSECRET=...`. Use the same secret on every client so one n8n credential covers them all, and let `company_id` and `host` in each event tell the clients apart.
- Existing installs: add to `sinks:` in config.yaml, then restart with `qbsync service stop` and `qbsync service start`:
  ```yaml
  webhook: {url: 'https://n8n.example.com/webhook/quickbooks', secret: '<shared secret>', send_status: errors}
  ```
- Run **Send Test Webhook** (`qbsync test-webhook`) while n8n's *Listen for test event* is on. That gives n8n a sample payload to build the rest of the workflow against.

**Events.** Every POST has a `type`. Branch on it with a Switch node:

| `type` | When | Payload |
|---|---|---|
| `changes` | after each sync, if anything changed | `changes[]`: new, changed and deleted records (`deleted: true`), in order, up to `batch_size` per request. The first sync sends everything, in batches |
| `reports` | when a report snapshot changed | `reports[]`: P&L, balance sheet, customer/vendor balances |
| `status` | when a sync fails or partly fails (`send_status: errors`), or after every sync (`always`) | `status` (`ok`, `partial` or `failed`), `error`, `errors[]`, `totals`. Use it to alert on clients whose QuickBooks sync has stopped working |
| `test` | `qbsync test-webhook` | a few sample records |

All events include `company_id`, `host`, `sent_at` and `source`. A `changes` record looks like this:

```json
{"entity": "Invoice", "id": "1A2B-1700000000", "label": "5057", "party": "Acme Dental",
 "txn_date": "2026-10-01", "amount": 990.0, "balance": 990.0, "deleted": false,
 "time_modified": "2026-10-01T10:22:33-05:00", "change_seq": 1234, "data": { ...full QuickBooks record... }}
```

**Delivery.** Changes are delivered at least once, in order:
- A failed request is retried after 2s, 10s and 30s, then again at the next sync. Nothing is skipped, because the cursor only moves forward after n8n returns a 2xx response.
- If n8n answers `413 Payload Too Large`, the batch size is halved automatically.
- Use `entity` + `id` as the upsert key on the n8n side, because a retried batch can arrive twice.

**Security.** Besides the header token, each request is signed with `X-QBSync-Signature: sha256=HMAC_SHA256(secret, raw_body)`, for receivers that want to verify the body.

**Answering questions on demand.** The push only goes one way. For an n8n AI agent that needs to look things up at any time, have the workflow store what it receives (n8n Data Tables or Postgres, or use the Postgres sink below) and point the agent at that store.

## Pushing to Postgres / Supabase

```yaml
sinks:
  postgres: {dsn: 'postgresql://user:pass@host:5432/db?sslmode=require'}
```

This sink creates `qb_records` and `qb_reports` (both keyed by `company_id`) and upserts into them, so several company files can share the same tables. Row-level security is turned on when the tables are created, so Supabase's public API can't read them until you add policies. It uses `pg8000` (pure Python), so it works in the 32-bit build.

Each sink keeps its own cursor and only moves it forward after a successful delivery. If a destination is down, the backlog is sent on the next run.

## Security notes

- Nothing listens on the network. QB Sync only makes outbound connections to the destinations you configure.
- The config file holds the webhook secret and Postgres password, and the SQLite file holds a copy of the financial data. In machine-wide installs the data folder is restricted to Administrators, SYSTEM and the run-as account. In per-user installs it's in the user's private profile.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `0x80040418` app has not accessed this file before | Do the first-time authorization above, as the same Windows user the task runs as |
| `0x8004040A` a different company file is open | QuickBooks is open on this PC with another company file. QB Sync uses an open session automatically when it's the same file (e.g. opened through a mapped drive), but never syncs a different company |
| `0x80040408` could not start QuickBooks | QuickBooks and QB Sync must run at the same privilege level: don't set QuickBooks to *Run as administrator*. If a connection attempt fails, QB Sync closes any QuickBooks it started in the background, so you can open QuickBooks normally afterwards |
| `0x80040430` unable to log in automatically | QuickBooks hasn't approved automatic login. With QuickBooks open as Admin, remove every QB Sync entry under *Edit → Preferences → Integrated Applications → Company Preferences*, save, then run **Test QuickBooks Connection** and choose *Yes, always; allow access even if QuickBooks is not running*. QB Sync asks for unattended, read-only access, so that's the only "yes" option offered |
| `0x80040440` can't return sensitive information without a complex password | Give the QuickBooks **Admin** and the QB Sync login user complex passwords (7+ characters with a number and an uppercase letter), then log in to QuickBooks once as the QB Sync user to get past QuickBooks' forced password change at first login |
| Works with QuickBooks open, `0x80040430` when closed | `company_file` must be exactly the path QuickBooks uses: a mapped drive (`Q:\...`) and a UNC path (`\\server\...`) count as different files. **Test QuickBooks Connection** with QuickBooks open prints `file_opened_by_quickbooks` and the line to put in the config. A mapped drive works for the background task because it runs in your logged-on session |
| `0x80040416` company file must be specified | Set `company_file` to the full UNC path |
| `0x8004041A` no permission | Integrated Applications preferences, allow QB Sync |
| `0x80040414` modal dialog showing | Someone has a QuickBooks dialog open on the sync host. Close it |
| *Class not registered* | Install the QuickBooks SDK, and/or use 32-bit Python |
| Webhook `HTTP 404` | The n8n workflow isn't active, or a *test* URL is in use while not listening |
| Webhook `HTTP 401/403` | The secret doesn't match the n8n Header Auth credential |
| One entity shows a status error in `qbsync status` | Not supported by your edition or version: add it to `sync.exclude`, or override its flags under `sync.entities` |

Logs are in the `logs` folder next to the config (Start Menu: *Open Data and Logs Folder*).

## Development (any OS)

`connection: mock` (or `qbsync init --mock`) runs everything against a simulated QuickBooks company (customers, items, invoices, payments, bills and a P&L), so you can build an n8n workflow without Windows.

```bash
pip install -e ".[dev]"
pytest                     # QBSYNC_TEST_PG=postgresql://... also runs the Postgres sink test
qbsync -c demo.yaml init --mock --webhook-url https://n8n.example.com/webhook-test/quickbooks
qbsync -c demo.yaml sync
```
