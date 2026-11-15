-- 修正版本 + 字段证据 + 主档确认（MySQL 8.0 语法，运行时不执行；建表由 SQLAlchemy create_all 完成）

-- 房源簇：受影响次数 / 是否当前簇 / 证据是否齐全
ALTER TABLE properties
    ADD COLUMN revision          INT     NOT NULL DEFAULT 0,
    ADD COLUMN is_current        BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN evidence_complete BOOLEAN NOT NULL DEFAULT FALSE;

-- 挂牌：当前生效的修正版本（应用层维护，不加外键）
ALTER TABLE listings
    ADD COLUMN current_correction_id INT NULL,
    ADD INDEX idx_listings_current_correction (current_correction_id);

-- 挂牌修正版本：每条修正追加一条，correction_key 幂等去重
CREATE TABLE listing_corrections (
    id                     INT AUTO_INCREMENT PRIMARY KEY,
    correction_key         VARCHAR(120) NOT NULL UNIQUE,
    listing_id             INT          NOT NULL,
    version                INT          NOT NULL DEFAULT 1,
    changes                JSON,                    -- [{field, old, new}]
    noop                   BOOLEAN      NOT NULL DEFAULT FALSE,
    source                 VARCHAR(20)  NOT NULL DEFAULT 'agency_feed',
    note                   TEXT,
    operator_email         VARCHAR(160) NOT NULL,
    run_id                 INT,
    affected_property_ids  JSON,
    invalidated_evidence   INT          NOT NULL DEFAULT 0,
    created_at             DATETIME     NOT NULL,
    CONSTRAINT fk_corrections_listing FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE,
    CONSTRAINT fk_corrections_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL
);

-- 主档字段证据：每个字段采信了哪家中介、哪条原文、哪个挂牌版本
CREATE TABLE field_evidences (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    property_id    INT          NOT NULL,
    field          VARCHAR(20)  NOT NULL,             -- area / price / layout
    value          VARCHAR(60)  NOT NULL,
    rule           VARCHAR(20)  NOT NULL,             -- mode / median
    listing_id     INT          NOT NULL,
    agency_id      INT          NOT NULL,
    correction_id  INT,                               -- NULL = 原始采集版本 v0
    source_text    VARCHAR(200) NOT NULL,
    status         VARCHAR(16)  NOT NULL DEFAULT 'valid',   -- valid / invalidated
    invalidated_by INT,
    invalidated_at DATETIME,
    run_id         INT,
    created_at     DATETIME     NOT NULL,
    CONSTRAINT fk_evidence_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    CONSTRAINT fk_evidence_listing FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE CASCADE,
    CONSTRAINT fk_evidence_agency FOREIGN KEY (agency_id) REFERENCES agencies(id) ON DELETE CASCADE,
    CONSTRAINT fk_evidence_correction FOREIGN KEY (correction_id) REFERENCES listing_corrections(id) ON DELETE SET NULL,
    CONSTRAINT fk_evidence_invalidated_by FOREIGN KEY (invalidated_by) REFERENCES listing_corrections(id) ON DELETE SET NULL,
    CONSTRAINT fk_evidence_run FOREIGN KEY (run_id) REFERENCES match_runs(id) ON DELETE SET NULL
);

-- 主档确认：按 revision 对齐；revision 落后即需重新确认
CREATE TABLE property_confirmations (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    property_id  INT          NOT NULL,
    revision     INT          NOT NULL DEFAULT 0,
    confirmed_by VARCHAR(160) NOT NULL,
    note         TEXT,
    created_at   DATETIME     NOT NULL,
    CONSTRAINT fk_confirmations_property FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE
);
