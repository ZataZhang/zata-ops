package com.dewell.keycloak.auth;

import com.aliyun.dypnsapi20170525.Client;
import com.aliyun.dypnsapi20170525.models.SendSmsVerifyCodeRequest;
import com.aliyun.dypnsapi20170525.models.SendSmsVerifyCodeResponse;
import com.aliyun.teaopenapi.models.Config;
import com.aliyun.teautil.models.RuntimeOptions;
import jakarta.ws.rs.core.MultivaluedMap;
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
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.email.EmailSenderProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.SingleUseObjectProvider;
import org.keycloak.models.UserCredentialModel;
import org.keycloak.models.UserModel;
import org.keycloak.sessions.AuthenticationSessionModel;
import org.keycloak.services.managers.BruteForceProtector;

final class SharedOtpAuthenticator implements Authenticator {
    private static final Logger LOG = Logger.getLogger(SharedOtpAuthenticator.class);
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Pattern EMAIL_PATTERN = Pattern.compile("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$");
    private static final Pattern PHONE_PATTERN = Pattern.compile("^1[3-9][0-9]{9}$");
    private static final String AUTH_NOTE_CHANNEL = "shared_otp_channel";
    private static final String AUTH_NOTE_RECIPIENT = "shared_otp_recipient";
    private static final String CACHE_PREFIX = "shared-keycloak-otp:";
    private static final int OTP_TTL_SECONDS = 300;
    private static final int RESEND_COOLDOWN_SECONDS = 60;
    private static final int MAX_OTP_ATTEMPTS = 5;
    private static final int MAX_REQUESTS_PER_IP_PER_MINUTE = 12;

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        context.challenge(context.form().createForm("shared-login.ftl"));
    }

    @Override
    public void action(AuthenticationFlowContext context) {
        MultivaluedMap<String, String> form = context.getHttpRequest().getDecodedFormParameters();
        String method = form.getFirst("method");
        if ("password".equals(method)) {
            authenticatePassword(context, form);
            return;
        }
        if ("email".equals(method) || "sms".equals(method)) {
            String step = form.getFirst("step");
            if ("request".equals(step)) {
                requestCode(context, method, form.getFirst("identifier"));
                return;
            }
            if ("verify".equals(step)) {
                verifyCode(context, form.getFirst("code"));
                return;
            }
        }
        challengeLoginError(context);
    }

    private void authenticatePassword(
        AuthenticationFlowContext context,
        MultivaluedMap<String, String> form
    ) {
        String identifier = trim(form.getFirst("identifier"));
        String password = form.getFirst("password");
        RealmModel realm = context.getRealm();
        KeycloakSession session = context.getSession();
        UserModel user = findUserByIdentifier(session, realm, identifier);
        if (user == null || !user.isEnabled()) {
            challengeLoginError(context);
            return;
        }
        BruteForceProtector protector = context.getProtector();
        if (protector.isTemporarilyDisabled(session, realm, user)
            || protector.isPermanentlyLockedOut(session, realm, user)) {
            challengeLoginError(context);
            return;
        }
        boolean valid = password != null
            && !password.isEmpty()
            && user.credentialManager().isValid(UserCredentialModel.password(password));
        if (!valid) {
            protector.failedLogin(
                realm,
                user,
                context.getConnection(),
                session.getContext().getUri(),
                java.util.Set.of("password")
            );
            challengeLoginError(context);
            return;
        }
        protector.successfulLogin(
            realm,
            user,
            context.getConnection(),
            session.getContext().getUri(),
            java.util.Set.of("password")
        );
        context.setUser(user);
        context.success();
    }

    private void requestCode(AuthenticationFlowContext context, String channel, String rawRecipient) {
        String recipient = normalizeRecipient(channel, rawRecipient);
        if (recipient == null && trim(rawRecipient).isEmpty()) {
            AuthenticationSessionModel authSession = context.getAuthenticationSession();
            String previousChannel = authSession.getAuthNote(AUTH_NOTE_CHANNEL);
            if (channel.equals(previousChannel)) {
                recipient = authSession.getAuthNote(AUTH_NOTE_RECIPIENT);
            }
        }
        if (recipient == null || !allowRequest(context)) {
            challengeLoginError(context);
            return;
        }
        RealmModel realm = context.getRealm();
        UserModel existingUser = findUserByChannel(context.getSession(), realm, channel, recipient);
        if (existingUser == null && !realm.isRegistrationAllowed()) {
            context.challenge(context.form().setInfo("sharedCodeRequestSent").createForm("shared-login.ftl"));
            return;
        }
        try {
            String challengeKey = challengeKey(realm, channel, recipient);
            SingleUseObjectProvider store = singleUseStore(context.getSession());
            Map<String, String> challengePointer = store.get(challengeKey);
            String cachedChallengeId = challengePointer == null
                ? null
                : challengePointer.get("challenge_id");
            Map<String, String> cachedChallenge = cachedChallengeId == null
                ? null
                : store.get(challengeDataKey(challengeKey, cachedChallengeId));
            long nowSeconds = System.currentTimeMillis() / 1000;
            boolean canResend = cachedChallenge == null
                || nowSeconds - parseLong(cachedChallenge.get("created_at"), 0)
                    >= RESEND_COOLDOWN_SECONDS;
            if (cachedChallenge == null || canResend) {
                String reservationKey = challengeKey + ":send-lock";
                if (store.putIfAbsent(reservationKey, (long) RESEND_COOLDOWN_SECONDS)) {
                    String verificationCode = newVerificationCode();
                    try {
                        sendCode(context, channel, recipient, verificationCode);
                    } catch (Exception deliveryError) {
                        // 不记录收件地址与异常消息（可能含地址）；释放冷却锁，避免重试命中中性提示而掩盖失败。
                        store.remove(reservationKey);
                        LOG.warnf(
                            "Shared OTP delivery failed: channel=%s, error=%s",
                            channel,
                            deliveryError.getClass().getName()
                        );
                        throw deliveryError;
                    }
                    String challengeId = UUID.randomUUID().toString();
                    Map<String, String> challengeValues = new HashMap<>();
                    challengeValues.put("code_hash", codeHash(realm, channel, recipient, verificationCode));
                    challengeValues.put("created_at", Long.toString(nowSeconds));
                    challengeValues.put("challenge_id", challengeId);
                    store.put(
                        challengeDataKey(challengeKey, challengeId),
                        OTP_TTL_SECONDS,
                        challengeValues
                    );
                    store.put(
                        challengeKey,
                        OTP_TTL_SECONDS,
                        Map.of("challenge_id", challengeId)
                    );
                } else if (cachedChallenge == null) {
                    context.challenge(context.form().setInfo("sharedCodeRequestSent").createForm("shared-login.ftl"));
                    return;
                }
            }
            AuthenticationSessionModel authSession = context.getAuthenticationSession();
            authSession.setAuthNote(AUTH_NOTE_CHANNEL, channel);
            authSession.setAuthNote(AUTH_NOTE_RECIPIENT, recipient);
            context.challenge(
                context.form()
                    .setAttribute("channel", channel)
                    .setAttribute("maskedRecipient", maskRecipient(channel, recipient))
                    .createForm("shared-code.ftl")
            );
        } catch (Exception exception) {
            challengeLoginError(context);
        }
    }

    private void verifyCode(AuthenticationFlowContext context, String suppliedCode) {
        AuthenticationSessionModel authSession = context.getAuthenticationSession();
        String channel = authSession.getAuthNote(AUTH_NOTE_CHANNEL);
        String recipient = authSession.getAuthNote(AUTH_NOTE_RECIPIENT);
        if ((!"email".equals(channel) && !"sms".equals(channel))
            || recipient == null
            || suppliedCode == null
            || !suppliedCode.matches("[0-9]{6}")) {
            challengeLoginError(context);
            return;
        }
        RealmModel realm = context.getRealm();
        SingleUseObjectProvider store = singleUseStore(context.getSession());
        String challengeKey = challengeKey(realm, channel, recipient);
        Map<String, String> challengePointer = store.get(challengeKey);
        String challengeId = challengePointer == null
            ? null
            : challengePointer.get("challenge_id");
        if (challengeId == null || challengeId.isBlank()) {
            challengeCodeError(context, channel, recipient);
            return;
        }
        String challengeDataKey = challengeDataKey(challengeKey, challengeId);
        Map<String, String> challenge = store.get(challengeDataKey);
        long nowSeconds = System.currentTimeMillis() / 1000;
        if (challenge == null
            || nowSeconds - parseLong(challenge.get("created_at"), 0) > OTP_TTL_SECONDS) {
            store.remove(challengeDataKey);
            challengeCodeError(context, channel, recipient);
            return;
        }
        if (!challengeId.equals(challenge.get("challenge_id"))) {
            store.remove(challengeDataKey);
            challengeCodeError(context, channel, recipient);
            return;
        }
        int attemptIndex = reserveExpiringSlot(
            store,
            challengeKey + ":attempt:" + challengeId,
            MAX_OTP_ATTEMPTS,
            OTP_TTL_SECONDS
        );
        if (attemptIndex < 0) {
            store.remove(challengeKey);
            challengeCodeError(context, channel, recipient);
            return;
        }
        String expectedHash = challenge.get("code_hash");
        String submittedHash = codeHash(realm, channel, recipient, suppliedCode);
        if (expectedHash == null || !MessageDigest.isEqual(
            expectedHash.getBytes(StandardCharsets.US_ASCII),
            submittedHash.getBytes(StandardCharsets.US_ASCII)
        )) {
            if (attemptIndex == MAX_OTP_ATTEMPTS - 1) {
                store.remove(challengeDataKey);
            }
            UserModel candidateUser = findUserByChannel(context.getSession(), realm, channel, recipient);
            if (candidateUser != null) {
                context.getProtector().failedLogin(
                    realm,
                    candidateUser,
                    context.getConnection(),
                    context.getSession().getContext().getUri(),
                    java.util.Set.of("otp")
                );
            }
            challengeCodeError(context, channel, recipient);
            return;
        }
        if (!consumeChallengeIfCurrent(store, challengeKey, challengeDataKey, challengeId)) {
            challengeCodeError(context, channel, recipient);
            return;
        }
        UserModel user = findUserByChannel(context.getSession(), realm, channel, recipient);
        if (user != null && !user.isEnabled()) {
            challengeLoginError(context);
            return;
        }
        if (user == null) {
            if (!realm.isRegistrationAllowed()) {
                challengeLoginError(context);
                return;
            }
            user = createVerifiedUser(context.getSession(), realm, channel, recipient);
        } else if ("email".equals(channel)) {
            user.setEmailVerified(true);
        } else {
            user.setSingleAttribute("phone_number", recipient);
            user.setSingleAttribute("phone_number_verified", "true");
        }
        if (user == null) {
            challengeLoginError(context);
            return;
        }
        context.getProtector().successfulLogin(
            realm,
            user,
            context.getConnection(),
            context.getSession().getContext().getUri(),
            java.util.Set.of("otp")
        );
        authSession.removeAuthNote(AUTH_NOTE_CHANNEL);
        authSession.removeAuthNote(AUTH_NOTE_RECIPIENT);
        context.setUser(user);
        context.success();
    }

    static UserModel createVerifiedUser(
        KeycloakSession session,
        RealmModel realm,
        String channel,
        String recipient
    ) {
        UserModel user = session.users().addUser(realm, recipient);
        user.setEnabled(true);
        if ("email".equals(channel)) {
            user.setEmail(recipient);
            user.setEmailVerified(true);
        } else {
            user.setSingleAttribute("phone_number", recipient);
            user.setSingleAttribute("phone_number_verified", "true");
        }
        return user;
    }

    private void sendCode(
        AuthenticationFlowContext context,
        String channel,
        String recipient,
        String code
    ) throws Exception {
        if ("email".equals(channel)) {
            String subject = "Your sign-in code";
            String textBody = "Your sign-in code is " + code + ". It expires in 5 minutes.";
            String htmlBody = "<p>Your sign-in code is <strong>" + code
                + "</strong>. It expires in 5 minutes.</p>";
            context.getSession().getProvider(EmailSenderProvider.class).send(
                context.getRealm().getSmtpConfig(),
                recipient,
                subject,
                textBody,
                htmlBody
            );
            return;
        }
        sendAliyunSms(recipient, code);
    }

    private void sendAliyunSms(String recipient, String code) throws Exception {
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
        SendSmsVerifyCodeResponse response = client.sendSmsVerifyCodeWithOptions(
            smsRequest,
            new RuntimeOptions()
        );
        if (response.getBody() == null
            || !Boolean.TRUE.equals(response.getBody().getSuccess())
            || !"OK".equals(response.getBody().getCode())) {
            throw new IllegalStateException("SMS provider rejected the message");
        }
    }

    private boolean allowRequest(AuthenticationFlowContext context) {
        try {
            String remoteAddress = context.getConnection().getRemoteAddr();
            if (remoteAddress == null || remoteAddress.isBlank()) {
                return false;
            }
            RealmModel realm = context.getRealm();
            String ipKeyPrefix = CACHE_PREFIX + "ip:" + hmac(realm, remoteAddress);
            SingleUseObjectProvider store = singleUseStore(context.getSession());
            return reserveExpiringSlot(
                store,
                ipKeyPrefix,
                MAX_REQUESTS_PER_IP_PER_MINUTE,
                60
            ) >= 0;
        } catch (RuntimeException exception) {
            return false;
        }
    }

    private static int reserveExpiringSlot(
        SingleUseObjectProvider store,
        String keyPrefix,
        int maximumSlots,
        int ttlSeconds
    ) {
        return ExpiringSlotLimiter.reserve(
            keyPrefix,
            maximumSlots,
            slotKey -> store.putIfAbsent(slotKey, (long) ttlSeconds)
        );
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

    private UserModel findUserByIdentifier(KeycloakSession session, RealmModel realm, String identifier) {
        if (identifier.isBlank()) {
            return null;
        }
        String lowerIdentifier = identifier.toLowerCase();
        if (EMAIL_PATTERN.matcher(lowerIdentifier).matches()) {
            return session.users().getUserByEmail(realm, lowerIdentifier);
        }
        String normalizedPhone = normalizePhone(lowerIdentifier);
        if (normalizedPhone != null) {
            return findUserByChannel(session, realm, "sms", normalizedPhone);
        }
        return session.users().getUserByUsername(realm, identifier);
    }

    private UserModel findUserByChannel(
        KeycloakSession session,
        RealmModel realm,
        String channel,
        String recipient
    ) {
        if ("email".equals(channel)) {
            return session.users().getUserByEmail(realm, recipient);
        }
        List<UserModel> matches = session.users()
            .searchForUserByUserAttributeStream(realm, "phone_number", recipient)
            .limit(2)
            .toList();
        return matches.size() == 1 ? matches.get(0) : null;
    }

    private static String normalizeRecipient(String channel, String value) {
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

    private static String normalizePhone(String value) {
        String digits = value.replaceAll("[\\s-]", "");
        if (digits.startsWith("+86")) {
            digits = digits.substring(3);
        } else if (digits.startsWith("0086")) {
            digits = digits.substring(4);
        }
        return PHONE_PATTERN.matcher(digits).matches() ? digits : null;
    }

    private static String newVerificationCode() {
        return String.format("%06d", RANDOM.nextInt(1_000_000));
    }

    private static SingleUseObjectProvider singleUseStore(KeycloakSession session) {
        return session.getProvider(SingleUseObjectProvider.class);
    }

    private static String challengeKey(RealmModel realm, String channel, String recipient) {
        return CACHE_PREFIX + "code:" + hmac(realm, realm.getId() + "|" + channel + "|" + recipient);
    }

    private static String challengeDataKey(String challengeKey, String challengeId) {
        return challengeKey + ":challenge:" + challengeId;
    }

    private static String codeHash(RealmModel realm, String channel, String recipient, String code) {
        return hmac(realm, realm.getId() + "|" + channel + "|" + recipient + "|" + code);
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

    private static String maskRecipient(String channel, String recipient) {
        if ("email".equals(channel)) {
            int separator = recipient.indexOf('@');
            String localPart = recipient.substring(0, separator);
            String visiblePart = localPart.length() <= 2 ? localPart.substring(0, 1) : localPart.substring(0, 2);
            return visiblePart + "***" + recipient.substring(separator);
        }
        return recipient.substring(0, 6) + "****" + recipient.substring(recipient.length() - 2);
    }

    private void challengeLoginError(AuthenticationFlowContext context) {
        context.challenge(
            context.form()
                .setError("sharedLoginFailed")
                .createForm("shared-login.ftl")
        );
    }

    private void challengeCodeError(AuthenticationFlowContext context, String channel, String recipient) {
        context.challenge(
            context.form()
                .setError("sharedCodeInvalid")
                .setAttribute("channel", channel)
                .setAttribute("maskedRecipient", maskRecipient(channel, recipient))
                .createForm("shared-code.ftl")
        );
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

    private static String trim(String value) {
        return value == null ? "" : value.trim();
    }

    private static String environment(String name) {
        return System.getenv().getOrDefault(name, "").trim();
    }

    private static String environmentOrDefault(String name, String fallback) {
        String value = environment(name);
        return value.isEmpty() ? fallback : value;
    }

    @Override
    public boolean requiresUser() {
        return false;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        return true;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
    }

    @Override
    public void close() {
    }
}
