ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS discovery_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS discovery_genres text[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS discovery_max_price numeric(8, 2),
    ADD COLUMN IF NOT EXISTS discovery_currency text NOT NULL DEFAULT 'USD';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'user_settings_discovery_max_price_check'
    ) THEN
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_discovery_max_price_check
            CHECK (discovery_max_price >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'user_settings_discovery_currency_check'
    ) THEN
        ALTER TABLE user_settings
            ADD CONSTRAINT user_settings_discovery_currency_check
            CHECK (discovery_currency IN ('USD', 'GBP', 'EUR'));
    END IF;
END
$$;
