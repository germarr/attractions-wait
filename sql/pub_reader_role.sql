-- The Serving Store's read-only role (ADR-0007).
--
-- The Azure Function App is internet-facing and only ever needs SELECT on ~12K
-- rows of published summaries, so it does not run as the database admin. This
-- role is what it authenticates as; the publisher on the collector host keeps a
-- separate, writing credential.
--
-- Run ONCE as an admin, substituting a generated password. The password is
-- stored only in the Function App's settings — not here, not in .env, not in
-- git. To rotate: re-run the ALTER below and update PGPASSWORD in the app
-- settings; nothing else changes.
--
--     az functionapp config appsettings set -n attractions-dashboard \
--        -g miscellaneous_projects --settings PGUSER=pub_reader PGPASSWORD='...'

-- CREATE ROLE pub_reader WITH LOGIN PASSWORD '<generated>';
-- ALTER  ROLE pub_reader WITH LOGIN PASSWORD '<generated>';   -- rotation

GRANT CONNECT ON DATABASE attractions TO pub_reader;
GRANT USAGE   ON SCHEMA   pub         TO pub_reader;
GRANT SELECT  ON ALL TABLES IN SCHEMA pub TO pub_reader;

-- So a new pub.* table added by a later publish pass is readable without
-- remembering to re-grant.
ALTER DEFAULT PRIVILEGES IN SCHEMA pub GRANT SELECT ON TABLES TO pub_reader;

-- PostgreSQL 14 grants PUBLIC the CREATE privilege on schema `public`, which
-- would let any role that can reach this database create objects in it. Deny it
-- here. (PostgreSQL 15 changed this default; this server is 14.)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Verified after applying, connecting as pub_reader:
--   ALLOWED  SELECT on pub.target_live / watermark / series_daily
--   denied   INSERT / UPDATE / DELETE / DROP on every pub table
--   denied   CREATE TABLE in schema public and schema pub
--   denied   SELECT on pg_authid (password hashes)
--
-- Known and accepted: PostgreSQL grants CONNECT on all databases to PUBLIC, so
-- pub_reader can open a connection to the other databases on this shared server
-- (football_prod, postgres). It can read ZERO tables in them — verified — but it
-- can occupy a connection slot. Revoking PUBLIC's CONNECT there would risk the
-- unrelated applications that depend on it, so it is deliberately left alone.
