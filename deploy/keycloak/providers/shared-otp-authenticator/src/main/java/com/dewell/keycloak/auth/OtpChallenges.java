package com.dewell.keycloak.auth;

import com.aliyun.dypnsapi20170525.Client;
import com.aliyun.dypnsapi20170525.models.SendSmsVerifyCodeRequest;
import com.aliyun.dypnsapi20170525.models.SendSmsVerifyCodeResponse;
import com.aliyun.teaopenapi.models.Config;
import com.aliyun.teautil.models.RuntimeOptions;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.jboss.logging.Logger;
import org.keycloak.email.EmailSenderProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.SingleUseObjectProvider;
import org.keycloak.models.UserModel;

/** Verification-code issuing and checking shared by sign-in and contact binding. */
final class OtpChallenges {
    static final String PURPOSE_LOGIN = "login";
    static final String PURPOSE_BIND = "bind";

    private static final Logger LOG = Logger.getLogger(OtpChallenges.class);
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Pattern EMAIL_PATTERN = Pattern.compile("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$");
    private static final Pattern PHONE_PATTERN = Pattern.compile("^1[3-9][0-9]{9}$");
    private static final String CACHE_PREFIX = "shared-keycloak-otp:";
    static final int OTP_TTL_SECONDS = 300;
    static final int RESEND_COOLDOWN_SECONDS = 60;
    static final int MAX_OTP_ATTEMPTS = 5;
    private static final int MAX_REQUESTS_PER_IP_PER_MINUTE = 12;

    enum IssueResult { SENT, STILL_VALID, COOLING_DOWN }

    enum VerifyResult { VERIFIED, EXPIRED, WRONG_CODE }

    private OtpChallenges() {
    }

    static IssueResult issue(
        KeycloakSession session,
        RealmModel realm,
        String purpose,
        String channel,
        String recipient
    ) throws Exception {
        String challengeKey = challengeKey(realm, purpose, channel, recipient);
        SingleUseObjectProvider store = singleUseStore(session);
        Map<String, String> challengePointer = store.get(challengeKey);
        String cachedChallengeId = challengePointer == null ? null : challengePointer.get("challenge_id");
        Map<String, String> cachedChallenge = cachedChallengeId == null
            ? null
            : store.get(challengeDataKey(challengeKey, cachedChallengeId));
        long nowSeconds = System.currentTimeMillis() / 1000;
        boolean canResend = cachedChallenge == null
            || nowSeconds - parseLong(cachedChallenge.get("created_at"), 0) >= RESEND_COOLDOWN_SECONDS;
        if (!canResend) {
            return IssueResult.STILL_VALID;
        }
        String reservationKey = challengeKey + ":send-lock";
        if (!store.putIfAbsent(reservationKey, (long) RESEND_COOLDOWN_SECONDS)) {
            return cachedChallenge == null ? IssueResult.COOLING_DOWN : IssueResult.STILL_VALID;
        }
        String verificationCode = String.format("%06d", RANDOM.nextInt(1_000_000));
        try {
            sendCode(session, realm, purpose, channel, recipient, verificationCode);
        } catch (Exception deliveryError) {
            // 不记录收件地址与异常消息（可能含地址）；释放冷却锁，避免重试命中中性提示而掩盖失败。
            store.remove(reservationKey);
            LOG.warnf(
                "Shared OTP delivery failed: purpose=%s, channel=%s, error=%s",
                purpose,
                channel,
                deliveryError.getClass().getName()
            );
            throw deliveryError;
        }
        String challengeId = UUID.randomUUID().toString();
        Map<String, String> challengeValues = new HashMap<>();
        challengeValues.put("code_hash", codeHash(realm, purpose, channel, recipient, verificationCode));
        challengeValues.put("created_at", Long.toString(nowSeconds));
        challengeValues.put("challenge_id", challengeId);
        store.put(challengeDataKey(challengeKey, challengeId), OTP_TTL_SECONDS, challengeValues);
        store.put(challengeKey, OTP_TTL_SECONDS, Map.of("challenge_id", challengeId));
        return IssueResult.SENT;
    }

    static VerifyResult verify(
        KeycloakSession session,
        RealmModel realm,
        String purpose,
        String channel,
        String recipient,
        String suppliedCode
    ) {
        if (suppliedCode == null || !suppliedCode.matches("[0-9]{6}")) {
            return VerifyResult.EXPIRED;
        }
        SingleUseObjectProvider store = singleUseStore(session);
        String challengeKey = challengeKey(realm, purpose, channel, recipient);
        Map<String, String> challengePointer = store.get(challengeKey);
        String challengeId = challengePointer == null ? null : challengePointer.get("challenge_id");
        if (challengeId == null || challengeId.isBlank()) {
            return VerifyResult.EXPIRED;
        }
        String challengeDataKey = challengeDataKey(challengeKey, challengeId);
        Map<String, String> challenge = store.get(challengeDataKey);
        long nowSeconds = System.currentTimeMillis() / 1000;
        if (challenge == null
            || nowSeconds - parseLong(challenge.get("created_at"), 0) > OTP_TTL_SECONDS
            || !challengeId.equals(challenge.get("challenge_id"))) {
            store.remove(challengeDataKey);
            return VerifyResult.EXPIRED;
        }
        int attemptIndex = ExpiringSlotLimiter.reserve(
            challengeKey + ":attempt:" + challengeId,
            MAX_OTP_ATTEMPTS,
            slotKey -> store.putIfAbsent(slotKey, (long) OTP_TTL_SECONDS)
        );
        if (attemptIndex < 0) {
            store.remove(challengeKey);
            return VerifyResult.EXPIRED;
        }
        String expectedHash = challenge.get("code_hash");
        String submittedHash = codeHash(realm, purpose, channel, recipient, suppliedCode);
        if (expectedHash == null || !MessageDigest.isEqual(
            expectedHash.getBytes(StandardCharsets.US_ASCII),
            submittedHash.getBytes(StandardCharsets.US_ASCII)
        )) {
            if (attemptIndex == MAX_OTP_ATTEMPTS - 1) {
                store.remove(challengeDataKey);
            }
            return VerifyResult.WRONG_CODE;
        }
        return consumeChallengeIfCurrent(store, challengeKey, challengeDataKey, challengeId)
            ? VerifyResult.VERIFIED
            : VerifyResult.EXPIRED;
    }

    static boolean consumeChallengeIfCurrent(
        SingleUseObjectProvider store,
        String currentChallengeKey,
        String challengeDataKey,
        String verifiedChallengeId
    ) {
        Map<String, String> consumedChallenge = store.remove(challengeDataKey);
        if (consumedChallenge == null
            || !verifiedChallengeId.equals(consumedChallenge.get("challenge_id"))) {
            return false;
        }
        Map<String, String> currentChallenge = store.get(currentChallengeKey);
        return currentChallenge != null
            && verifiedChallengeId.equals(currentChallenge.get("challenge_id"));
    }

    static boolean allowRequest(KeycloakSession session, RealmModel realm, String remoteAddress) {
        try {
            if (remoteAddress == null || remoteAddress.isBlank()) {
                return false;
            }
            SingleUseObjectProvider store = singleUseStore(session);
            return ExpiringSlotLimiter.reserve(
                CACHE_PREFIX + "ip:" + hmac(realm, remoteAddress),
                MAX_REQUESTS_PER_IP_PER_MINUTE,
                slotKey -> store.putIfAbsent(slotKey, 60L)
            ) >= 0;
        } catch (RuntimeException exception) {
            return false;
        }
    }

    static UserModel findUserByChannel(
        KeycloakSession session,
        RealmModel realm,
        String channel,
        String recipient
    ) {
        if ("email".equals(channel)) {
            // 验证码只证明收件人拥有该邮箱；未验证的邮箱可能是他人填写的，不能借此登录那个账号。
            UserModel emailUser = session.users().getUserByEmail(realm, recipient);
            return emailUser != null && emailUser.isEmailVerified() ? emailUser : null;
        }
        List<UserModel> matches = session.users()
            .searchForUserByUserAttributeStream(realm, "phone_number", recipient)
            .limit(2)
            .toList();
        return matches.size() == 1 ? matches.get(0) : null;
    }

    /** Returns true when another account already holds the contact, verified or not. */
    static boolean heldByAnotherUser(
        KeycloakSession session,
        RealmModel realm,
        String channel,
        String recipient,
        UserModel currentUser
    ) {
        if ("email".equals(channel)) {
            UserModel emailUser = session.users().getUserByEmail(realm, recipient);
            return emailUser != null && !emailUser.getId().equals(currentUser.getId());
        }
        return session.users()
            .searchForUserByUserAttributeStream(realm, "phone_number", recipient)
            .anyMatch(phoneUser -> !phoneUser.getId().equals(currentUser.getId()));
    }

    static String normalizeRecipient(String channel, String value) {
        if (value == null) {
            return null;
        }
        String normalizedValue = value.trim().toLowerCase();
        if ("email".equals(channel)) {
            return normalizedValue.length() <= 255 && EMAIL_PATTERN.matcher(normalizedValue).matches()
                ? normalizedValue
                : null;
        }
        String normalizedPhone = normalizePhone(normalizedValue);
        return normalizedPhone == null ? null : "+86" + normalizedPhone;
    }

    static boolean looksLikeEmail(String value) {
        return EMAIL_PATTERN.matcher(value).matches();
    }

    static String normalizePhone(String value) {
        String digits = value.replaceAll("[\\s-]", "");
        if (digits.startsWith("+86")) {
            digits = digits.substring(3);
        } else if (digits.startsWith("0086")) {
            digits = digits.substring(4);
        }
        return PHONE_PATTERN.matcher(digits).matches() ? digits : null;
    }

    static String maskRecipient(String channel, String recipient) {
        if ("email".equals(channel)) {
            int separator = recipient.indexOf('@');
            String localPart = recipient.substring(0, separator);
            String visiblePart = localPart.length() <= 2 ? localPart.substring(0, 1) : localPart.substring(0, 2);
            return visiblePart + "***" + recipient.substring(separator);
        }
        return recipient.substring(0, 6) + "****" + recipient.substring(recipient.length() - 2);
    }

    private static void sendCode(
        KeycloakSession session,
        RealmModel realm,
        String purpose,
        String channel,
        String recipient,
        String code
    ) throws Exception {
        if ("email".equals(channel)) {
            String noun = PURPOSE_BIND.equals(purpose) ? "verification code" : "sign-in code";
            String textBody = "Your " + noun + " is " + code + ". It expires in 5 minutes.";
            String htmlBody = "<p>Your " + noun + " is <strong>" + code + "</strong>. It expires in 5 minutes.</p>";
            session.getProvider(EmailSenderProvider.class).send(
                realm.getSmtpConfig(),
                recipient,
                "Your " + noun,
                textBody,
                htmlBody
            );
            return;
        }
        sendAliyunSms(recipient, code);
    }

    private static void sendAliyunSms(String recipient, String code) throws Exception {
        String accessKeyId = environment("KC_OTP_SMS_ACCESS_KEY_ID");
        String accessKeySecret = environment("KC_OTP_SMS_ACCESS_KEY_SECRET");
        String signName = environment("KC_OTP_SMS_SIGN_NAME");
        String templateCode = environment("KC_OTP_SMS_TEMPLATE_CODE");
        if (accessKeyId.isBlank() || accessKeySecret.isBlank() || signName.isBlank() || templateCode.isBlank()) {
            throw new IllegalStateException("SMS provider is not configured");
        }
        Config clientConfig = new Config()
            .setAccessKeyId(accessKeyId)
            .setAccessKeySecret(accessKeySecret)
            .setEndpoint(environmentOrDefault("KC_OTP_SMS_ENDPOINT", "dypnsapi.aliyuncs.com"))
            .setProtocol(environmentOrDefault("KC_OTP_SMS_PROTOCOL", "https"));
        Client client = new Client(clientConfig);
        SendSmsVerifyCodeRequest smsRequest = new SendSmsVerifyCodeRequest()
            .setPhoneNumber(recipient.substring(3))
            .setCountryCode("86")
            .setSignName(signName)
            .setTemplateCode(templateCode)
            .setTemplateParam("{\"code\":\"" + code + "\",\"min\":\"5\"}")
            .setInterval((long) RESEND_COOLDOWN_SECONDS)
            .setValidTime((long) OTP_TTL_SECONDS)
            .setCodeType(1L)
            .setReturnVerifyCode(false);
        SendSmsVerifyCodeResponse response = client.sendSmsVerifyCodeWithOptions(smsRequest, new RuntimeOptions());
        if (response.getBody() == null
            || !Boolean.TRUE.equals(response.getBody().getSuccess())
            || !"OK".equals(response.getBody().getCode())) {
            throw new IllegalStateException("SMS provider rejected the message");
        }
    }

    private static SingleUseObjectProvider singleUseStore(KeycloakSession session) {
        return session.getProvider(SingleUseObjectProvider.class);
    }

    private static String challengeKey(RealmModel realm, String purpose, String channel, String recipient) {
        return CACHE_PREFIX + "code:" + hmac(realm, realm.getId() + "|" + purpose + "|" + channel + "|" + recipient);
    }

    private static String challengeDataKey(String challengeKey, String challengeId) {
        return challengeKey + ":challenge:" + challengeId;
    }

    private static String codeHash(RealmModel realm, String purpose, String channel, String recipient, String code) {
        return hmac(realm, realm.getId() + "|" + purpose + "|" + channel + "|" + recipient + "|" + code);
    }

    private static String hmac(RealmModel realm, String value) {
        String secret = environment("KC_SHARED_OTP_HMAC_SECRET");
        if (secret.length() < 32) {
            throw new IllegalStateException("OTP HMAC secret is missing or too short");
        }
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(value.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) {
            throw new IllegalStateException("OTP HMAC unavailable", exception);
        }
    }

    private static long parseLong(String value, long fallback) {
        if (value == null) {
            return fallback;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException exception) {
            return fallback;
        }
    }

    private static String environment(String name) {
        return System.getenv().getOrDefault(name, "").trim();
    }

    private static String environmentOrDefault(String name, String fallback) {
        String value = environment(name);
        return value.isEmpty() ? fallback : value;
    }
}
