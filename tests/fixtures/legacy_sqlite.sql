-- Original schema at a3fffb6, before the expo app changes.
CREATE TABLE companies (
	id INTEGER NOT NULL, 
	name VARCHAR(300) NOT NULL, 
	ats_type VARCHAR(40) NOT NULL, 
	slug VARCHAR(200), 
	workday_tenant VARCHAR(200), 
	workday_site VARCHAR(200), 
	resolved BOOLEAN NOT NULL, 
	source_hint VARCHAR(100), 
	last_checked_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE filters (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	sectors TEXT NOT NULL, 
	keywords TEXT NOT NULL, 
	exclude_keywords TEXT NOT NULL, 
	locations TEXT NOT NULL, 
	remote_only BOOLEAN NOT NULL, 
	channels TEXT NOT NULL, 
	active BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE postings (
	id INTEGER NOT NULL, 
	external_id VARCHAR(200), 
	company_id INTEGER, 
	company_name VARCHAR(300) NOT NULL, 
	title VARCHAR(500) NOT NULL, 
	url TEXT NOT NULL, 
	location TEXT, 
	remote BOOLEAN NOT NULL, 
	source VARCHAR(50) NOT NULL, 
	category_hint VARCHAR(120), 
	sector_tags TEXT NOT NULL, 
	term VARCHAR(120), 
	posted_at DATETIME, 
	first_seen_at DATETIME NOT NULL, 
	raw_hash VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_posting_raw_hash UNIQUE (raw_hash), 
	FOREIGN KEY(company_id) REFERENCES companies (id)
);

CREATE INDEX ix_postings_company_name ON postings (company_name);

CREATE INDEX ix_postings_first_seen_at ON postings (first_seen_at);

CREATE TABLE alerts_sent (
	id INTEGER NOT NULL, 
	posting_id INTEGER NOT NULL, 
	filter_id INTEGER NOT NULL, 
	channel VARCHAR(30) NOT NULL, 
	sent_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_alert_once_per_channel UNIQUE (posting_id, filter_id, channel), 
	FOREIGN KEY(posting_id) REFERENCES postings (id), 
	FOREIGN KEY(filter_id) REFERENCES filters (id)
);
