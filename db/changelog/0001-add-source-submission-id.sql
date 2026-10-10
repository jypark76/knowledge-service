--liquibase formatted sql

--changeset denny:0001-add-source-submission-id
-- In plain English: gives every saved example a place to write which submission it
-- came from, and refuses to save two examples with the same one. The assessment
-- service will send this label with every approved example. If the same message
-- arrives twice (Kafka can deliver a message more than once), the second one is
-- recognised and not saved again.
--
-- The column is allowed to be empty on purpose: examples saved before this change,
-- and examples saved by hand, have no label. The uniqueness rule treats empty as
-- "no label", so any number of those can exist side by side.
ALTER TABLE examples ADD COLUMN source_submission_id uuid;
CREATE UNIQUE INDEX examples_source_submission_id_key ON examples (source_submission_id);
--rollback DROP INDEX examples_source_submission_id_key;
--rollback ALTER TABLE examples DROP COLUMN source_submission_id;
