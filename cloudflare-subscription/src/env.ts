export interface Env {
  DB: D1Database;
  /** 설정 페이지 출처(예: https://law-rader-subscription.xxx.workers.dev). wrangler.jsonc vars. */
  PUBLIC_ORIGIN: string;
  /** Python ↔ Worker 내부 API Bearer 토큰(Secret, 32자 이상). GitHub Secret 과 같은 값. */
  INTERNAL_API_TOKEN: string;
  /** 구독자 식별자 HMAC 키(Secret, 32자 이상). GitHub Secret 과 같은 값. 교체 금지. */
  SUBSCRIBER_HASH_KEY: string;
  /** 인증번호 해시·요청 제한 키 파생용 서버 비밀(Secret, 32자 이상). Worker 에만 있다. */
  SERVER_SECRET: string;
  /** Apps Script 웹앱 /exec 주소(Secret). */
  APPS_SCRIPT_URL: string;
  /** Worker ↔ Apps Script 요청 서명 키(Secret, 32자 이상). Apps Script 스크립트 속성과 같은 값. */
  APPS_SCRIPT_SHARED_SECRET: string;
}

const MIN_SECRET_CHARS = 32;

/** 비밀값이 비었거나 짧으면 해당 기능을 '설정 오류'로 거절한다(약한 값으로 동작하지 않음). */
export function strongSecret(value: string | undefined): value is string {
  return typeof value === "string" && value.length >= MIN_SECRET_CHARS;
}
