-- Scoped experiment function, installed only in the driver's disposable database.
CREATE OR REPLACE FUNCTION nf_hera_count_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND (OLD.work_id, OLD.branch_id) = (NEW.work_id, NEW.branch_id) THEN
    IF OLD.state = NEW.state THEN
      RETURN NULL;
    END IF;
    UPDATE nf_hera_progress
    SET counts = jsonb_set(jsonb_set(counts, ARRAY[OLD.state::text],
                           to_jsonb((counts ->> OLD.state)::bigint - 1)),
                           ARRAY[NEW.state::text],
                           to_jsonb(coalesce((counts ->> NEW.state)::bigint, 0) + 1)),
        watermark = watermark + 2
    WHERE work_id = NEW.work_id AND branch_id = NEW.branch_id;
  ELSE
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
      UPDATE nf_hera_progress
      SET counts = jsonb_set(counts, ARRAY[OLD.state::text], to_jsonb((counts ->> OLD.state)::bigint - 1)),
          watermark = watermark + 1
      WHERE work_id = OLD.work_id AND branch_id = OLD.branch_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
      UPDATE nf_hera_progress
      SET counts = jsonb_set(counts, ARRAY[NEW.state::text],
                             to_jsonb(coalesce((counts ->> NEW.state)::bigint, 0) + 1)),
          watermark = watermark + 1
      WHERE work_id = NEW.work_id AND branch_id = NEW.branch_id;
    END IF;
  END IF;
  RETURN NULL;
EXCEPTION WHEN data_exception OR not_null_violation THEN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    DELETE FROM nf_hera_progress WHERE work_id = OLD.work_id AND branch_id = OLD.branch_id;
  END IF;
  IF TG_OP IN ('UPDATE', 'INSERT') THEN
    DELETE FROM nf_hera_progress WHERE work_id = NEW.work_id AND branch_id = NEW.branch_id;
  END IF;
  RETURN NULL;
END $$;
