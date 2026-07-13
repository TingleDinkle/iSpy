CREATE OR REPLACE VIEW v_latest_metrics AS
SELECT DISTINCT ON (s.app_id)
    a.store, a.store_app_id, a.name, a.developer_name, a.tier,
    s.snapshot_date, s.version, s.rating_value, s.rating_count,
    s.revenue_monthly_est, s.downloads_monthly_est
FROM app_snapshots s
JOIN apps a ON a.id = s.app_id
WHERE a.is_active
ORDER BY s.app_id, s.snapshot_date DESC;

CREATE OR REPLACE VIEW v_metric_history AS
SELECT a.store, a.store_app_id, a.name, s.snapshot_date,
       s.revenue_monthly_est, s.downloads_monthly_est,
       s.rating_value, s.rating_count, s.version
FROM app_snapshots s
JOIN apps a ON a.id = s.app_id;

CREATE OR REPLACE VIEW v_rank_history AS
SELECT r.store, r.date, r.country, r.category, r.collection, r.platform,
       r.rank, r.store_app_id, a.name, a.tier
FROM rankings r
LEFT JOIN apps a ON a.store = r.store AND a.store_app_id = r.store_app_id;

CREATE OR REPLACE VIEW v_events_feed AS
SELECT e.event_date, e.event_type, e.title,
       COALESCE(a.name, e.store_app_id) AS app_name,
       e.store, e.details, e.created_at, e.notified_at
FROM app_events e
LEFT JOIN apps a ON a.id = e.app_id
ORDER BY e.event_date DESC, e.created_at DESC;

CREATE OR REPLACE VIEW v_alerts_feed AS
SELECT al.alert_date, al.metric, a.name AS app_name, a.store,
       al.value, al.baseline, al.pct_change, al.window_days, al.created_at
FROM alerts al
JOIN apps a ON a.id = al.app_id
ORDER BY al.alert_date DESC;

CREATE OR REPLACE VIEW v_review_topics_weekly AS
SELECT a.store, a.name AS app_name, topic.value AS topic,
       date_trunc('week', r.created_at)::date AS week,
       count(*) AS mentions,
       avg(r.stars)::numeric(3,2) AS avg_stars
FROM reviews r
JOIN apps a ON a.id = r.app_id
CROSS JOIN LATERAL jsonb_array_elements_text(r.topics) AS topic(value)
WHERE r.topics IS NOT NULL AND r.created_at IS NOT NULL
GROUP BY a.store, a.name, topic.value, date_trunc('week', r.created_at);

CREATE OR REPLACE VIEW v_rating_weekly AS
SELECT a.store, a.name AS app_name,
       date_trunc('week', r.created_at)::date AS week,
       count(*) AS reviews,
       avg(r.stars)::numeric(3,2) AS avg_stars
FROM reviews r
JOIN apps a ON a.id = r.app_id
WHERE r.created_at IS NOT NULL
GROUP BY a.store, a.name, date_trunc('week', r.created_at);

CREATE OR REPLACE VIEW v_market_history AS
SELECT seg.name AS segment, seg.store, m.date,
       m.available, m.downloads, m.revenue, m.ipd, m.total, m.removed
FROM market_snapshots m
JOIN market_segments seg ON seg.id = m.segment_id
ORDER BY seg.name, m.date;
