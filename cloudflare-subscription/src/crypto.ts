// 암호 유틸 — Web Crypto(표준 API)만 쓴다.
//
// normalizeEmail / subscriberId 는 Python src/subscription.py 의 normalize_email /
// subscriber_hash 와 결과가 바이트 단위로 같아야 한다(API v1 계약). 공통 테스트 벡터:
// test/fixtures/hmac_vectors.json (Python 테스트도 같은 파일을 검사한다).

const encoder = new TextEncoder();

export function toHex(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let out = "";
  for (const b of view) out += b.toString(16).padStart(2, "0");
  return out;
}

export async function hmacHex(key: string, message: string): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return toHex(await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(message)));
}

export async function sha256Hex(message: string): Promise<string> {
  return toHex(await crypto.subtle.digest("SHA-256", encoder.encode(message)));
}

export function randomHex(byteLength: number): string {
  return toHex(crypto.getRandomValues(new Uint8Array(byteLength)));
}

/** 세션 토큰: 256비트 난수의 base64url. */
export function randomToken(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 6자리 인증번호. 거부 표본추출로 모듈로 편향을 없앤다. */
export function randomOtp(): string {
  const range = 1_000_000;
  const limit = 2 ** 32 - (2 ** 32 % range);
  const buf = new Uint32Array(1);
  for (;;) {
    crypto.getRandomValues(buf);
    if (buf[0] < limit) return String(buf[0] % range).padStart(6, "0");
  }
}

/** 같은 길이의 문자열을 상수 시간으로 비교한다(길이가 다르면 즉시 false — 길이는 비밀이 아니다). */
export function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Python normalize_email 과 같은 규칙: 앞뒤 공백 제거 → '@' 정확히 1개, 양쪽 비어 있지
 * 않음 → ASCII 만 → 공백·제어문자(0x00-0x20, 0x7F) 없음 → ASCII 소문자.
 * 규칙에 맞지 않으면 null(주소를 오류 메시지·로그에 싣지 않기 위해 예외 대신 null).
 */
export function normalizeEmail(input: unknown): string | null {
  if (typeof input !== "string" || input.length > 320) return null;
  const text = input.trim();
  const at = text.indexOf("@");
  if (at <= 0 || at === text.length - 1 || text.indexOf("@", at + 1) !== -1) return null;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c > 0x7e || c < 0x21) return null;
  }
  return text.toLowerCase();
}

/** HMAC-SHA256(SUBSCRIBER_HASH_KEY, 정규화 주소) 소문자 hex. */
export async function subscriberId(normalizedEmail: string, key: string): Promise<string> {
  return hmacHex(key, normalizedEmail);
}
