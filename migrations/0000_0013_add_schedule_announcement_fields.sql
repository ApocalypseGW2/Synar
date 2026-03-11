ALTER TABLE schedules ADD COLUMN ping_roles INTEGER NOT NULL DEFAULT 0;
ALTER TABLE schedules ADD COLUMN announcement_message TEXT;
