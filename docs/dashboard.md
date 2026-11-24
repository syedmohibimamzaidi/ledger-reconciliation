# Ledger dashboard

Run `make setup` and follow the [API setup](api.md#setup-and-http-walkthrough) to
configure PostgreSQL, migrate the database, set the demo token, and start the API.
Open <http://127.0.0.1:8000/dashboard/> and enter the same demo token. FastAPI serves
the dashboard and the authenticated API from the same origin. The browser keeps
the token in memory until you disconnect or reload the page.

The **Run reconciliation** button submits a background job. To enable it, start
Redis, configure `REDIS_URL` for the API, and run `make worker` and `make dispatcher`
in separate terminals with the same database and Redis settings. See the
[background reconciliation setup](background-reconciliation.md) for commands.

1. Create or select an account. The sidebar shows the account used for each action.
2. In **Receipts**, enter structured receipt details. Leave assignment checked
   to assign to the selected account, or use **Unassigned receipts** and assign
   later from the record's details.
3. In **Transactions**, import a bank CSV. The import reports inserted and skipped
   rows and displays validation errors with their row numbers.
4. Run reconciliation from **Overview** or **Jobs**. The selected job is polled
   until it succeeds or fails. Successful execution can still produce review cases.
5. In **Review**, compare the receipt with its candidate, enter a reason, then
   confirm or reject. Confirming a different amount requires explicit
   acknowledgement. If the case changed, reload it before deciding.
6. In **Audit history**, expand events to see their recorded evidence and open the
   related records. Events sharing a timestamp are grouped on the current page.

The Overview counts come from the account summary. It shows stored decisions
separately from new inputs since the reconciliation cutoff and excludes credits
and zero amounts from unmatched debits. The review queue loads on navigation or
refresh. New jobs or audit events can shift offset pages while browsing.
