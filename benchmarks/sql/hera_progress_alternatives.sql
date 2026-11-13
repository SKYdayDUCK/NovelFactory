-- Mechanism probes on one branch. Only READY <-> PAUSED updates are driven.
CREATE TABLE bench_progress_shard (
    work_id uuid NOT NULL, branch_id uuid NOT NULL, shard integer NOT NULL,
    ready integer NOT NULL, paused integer NOT NULL,
    PRIMARY KEY(work_id, branch_id, shard)
);
CREATE FUNCTION bench_task_shard(id uuid) RETURNS integer IMMUTABLE PARALLEL SAFE
    LANGUAGE sql AS $$ SELECT get_byte(uuid_send(id), 15) % 16 $$;
CREATE FUNCTION bench_sharded_delta() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d integer := (NEW.state = 'READY')::int - (OLD.state = 'READY')::int;
BEGIN
    UPDATE bench_progress_shard SET ready = ready + d, paused = paused - d
    WHERE work_id = NEW.work_id AND branch_id = NEW.branch_id AND shard = bench_task_shard(NEW.task_id);
    RETURN NULL;
END $$;
CREATE TABLE bench_progress_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, work_id uuid NOT NULL,
    branch_id uuid NOT NULL, ready_delta integer NOT NULL
);
CREATE INDEX ON bench_progress_outbox(work_id, branch_id);
CREATE TABLE bench_progress_checkpoint (
    work_id uuid, branch_id uuid, ready integer NOT NULL, paused integer NOT NULL,
    last_folded_id bigint NOT NULL, PRIMARY KEY(work_id, branch_id)
);
CREATE FUNCTION bench_enqueue_delta() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bench_progress_outbox(work_id, branch_id, ready_delta)
    VALUES (NEW.work_id, NEW.branch_id, (NEW.state = 'READY')::int - (OLD.state = 'READY')::int);
    RETURN NULL;
END $$;
