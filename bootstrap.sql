-- zerocrm one-time privileged bootstrap.
--
-- Run ONCE by a DB admin (Supabase postgres role). Creates the isolated schema
-- and the least-privilege login role the engine runs as. After this, the engine
-- connects AS zerocrm_engine and runs its OWN migrations (migrate.py) to create
-- tables — which it then owns, so it has full rights on its own data and NONE
-- anywhere else.
--
-- Security property (spec D3): zerocrm_engine is granted privileges ONLY inside
-- the zerocrm schema. It is a member of no other role and holds no grants on
-- any other schema, so it cannot read or write data outside zerocrm even if its
-- credential leaks. No co-tenant schema is ever touched.
--
-- Deliberately applied OUT-OF-BAND (not via the shared release train, spec
-- D11.6) so a zerocrm change can never enter another app's migration history or
-- red a product deploy.

CREATE SCHEMA IF NOT EXISTS zerocrm AUTHORIZATION postgres;

-- :engine_password is supplied at apply time from a freshly generated secret;
-- it is never committed and never written to migration history.
CREATE ROLE zerocrm_engine LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  PASSWORD :'engine_password';

-- full rights INSIDE zerocrm (USAGE to see it, CREATE so migrate() can build
-- tables it will then own). Nothing is granted outside this schema.
GRANT USAGE, CREATE ON SCHEMA zerocrm TO zerocrm_engine;
