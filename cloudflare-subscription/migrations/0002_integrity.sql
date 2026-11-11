-- 설정 유실·DB 교체 감지(Phase 2.6)
--
-- 1) pref_saved: 한 번이라도 설정을 저장한 구독자. 이 표시가 있는데 preferences 행이 없으면
--    '설정 유실'로 보고 내부 API 가 정상 응답하지 않는다(기본값 mode=all 로 조용히 돌아가지 않음).
--    신규 구독자는 0 이라 지금처럼 mode=all 이 초기값이다.
ALTER TABLE subscribers ADD COLUMN pref_saved INTEGER NOT NULL DEFAULT 0 CHECK (pref_saved IN (0, 1));
UPDATE subscribers SET pref_saved = 1 WHERE subscriber IN (SELECT subscriber FROM preferences);

-- 2) service_meta: 이 D1 이 어떤 운영 데이터인지 나타내는 값.
--    instance  — 최초 동기화 때 만든 무작위 식별자. 운영자가 GitHub Variable SUBSCRIPTION_DB_INSTANCE 에
--                옮겨 두면, D1 이 재생성·교체되어 값이 없거나 다를 때 발송을 멈춘다.
--    key_check — 최초 동기화 때의 SUBSCRIBER_HASH_KEY 확인값. GitHub·Worker 키가 함께 바뀌면 다르다.
CREATE TABLE service_meta (
  k  TEXT PRIMARY KEY CHECK (k IN ('instance', 'key_check')),
  v  TEXT NOT NULL CHECK (length(v) BETWEEN 32 AND 64)
);
