# OpenIMIS GraphQL to Raw SQL Adapters

## Overview
This repository contains the custom Raw SQL Adapters developed for the openIMIS backend. 

### Why were they built?
By default, the openIMIS backend uses Graphene-Django and the standard Django ORM. While this is great for rapid development, it introduces two massive performance bottlenecks on large Microsoft SQL Server databases:
1. **The `_Icontains` Problem:** The ORM casts strings to `NVARCHAR(MAX)` or similar when executing `LIKE` queries. This totally breaks SQL Server indexing, resulting in slow full-table scans.
2. **Database Locking:** The ORM executes complex `SELECT` queries that place Shared Locks on tables. If someone is reading data, it blocks administrators from writing to that data (and vice-versa).

### The Solution
These adapters bypass the standard Django ORM entirely for heavy list queries (e.g., `claims`, `insurees`, `families`, `policies`). 
They dynamically parse incoming GraphQL arguments and construct a raw SQL query that:
1. Translates `__icontains` to `__istartswith` where appropriate to leverage indexes.
2. Employs `WITH (NOLOCK)` on every single `JOIN` and `SELECT` to guarantee zero database blocking.
3. Retrieves only the exact matching Primary Key IDs, and feeds them back into Graphene so the frontend receives the exact same GraphQL data structure in a fraction of the time.

## Included Adapters
- `claim_raw_sql_adapter.py`: Optimized for Claims (`tblClaim` and `tblICDCodes`).
- `insuree_raw_sql_adapter.py`: Optimized for Insurees (`tblInsuree` and `tblLocations`).
- `family_raw_sql_adapter.py`: Optimized for Families (`tblFamilies`).
- `policy_raw_sql_adapter.py`: Optimized for Policies (`tblPolicy`, `tblProduct`, etc.).

*All adapters feature a custom `decode_relay_id` engine to correctly translate Base64 Relay IDs sent by the frontend (like `icd_Id: "RGlhZ25vc2lzR1FMVHlwZToxMjA3Mw=="`) back to integers.*

## Deployment & Usage
To deploy an adapter, simply drop it into the respective module and intercept the Graphene field in `schema.py`. 

### The Feature Toggle
All adapters are strictly guarded by a feature toggle. To activate them, you must set this environment variable on your backend server:
```bash
USE_RAW_SQL_ADAPTOR=True
```
If this is set to `False` (or absent), the system will safely fall back to the standard Django ORM.
