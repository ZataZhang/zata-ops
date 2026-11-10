package com.dewell.keycloak.auth;

import jakarta.ws.rs.core.MultivaluedMap;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.Authenticator;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserCredentialModel;
import org.keycloak.models.UserModel;
import org.keycloak.sessions.AuthenticationSessionModel;
import org.keycloak.services.managers.BruteForceProtector;

final class SharedOtpAuthenticator implements Authenticator {
    private static final String AUTH_NOTE_CHANNEL = "shared_otp_channel";
    private static final String AUTH_NOTE_RECIPIENT = "shared_otp_recipient";

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
        String recipient = OtpChallenges.normalizeRecipient(channel, rawRecipient);
        if (recipient == null && trim(rawRecipient).isEmpty()) {
            AuthenticationSessionModel authSession = context.getAuthenticationSession();
            String previousChannel = authSession.getAuthNote(AUTH_NOTE_CHANNEL);
            if (channel.equals(previousChannel)) {
                recipient = authSession.getAuthNote(AUTH_NOTE_RECIPIENT);
            }
        }
        RealmModel realm = context.getRealm();
        KeycloakSession session = context.getSession();
        if (recipient == null
            || !OtpChallenges.allowRequest(session, realm, context.getConnection().getRemoteAddr())) {
            challengeLoginError(context);
            return;
        }
        UserModel existingUser = OtpChallenges.findUserByChannel(session, realm, channel, recipient);
        if (existingUser == null && !realm.isRegistrationAllowed()) {
            context.challenge(context.form().setInfo("sharedCodeRequestSent").createForm("shared-login.ftl"));
            return;
        }
        try {
            OtpChallenges.IssueResult issueResult = OtpChallenges.issue(
                session,
                realm,
                OtpChallenges.PURPOSE_LOGIN,
                channel,
                recipient
            );
            if (issueResult == OtpChallenges.IssueResult.COOLING_DOWN) {
                context.challenge(context.form().setInfo("sharedCodeRequestSent").createForm("shared-login.ftl"));
                return;
            }
            AuthenticationSessionModel authSession = context.getAuthenticationSession();
            authSession.setAuthNote(AUTH_NOTE_CHANNEL, channel);
            authSession.setAuthNote(AUTH_NOTE_RECIPIENT, recipient);
            context.challenge(
                context.form()
                    .setAttribute("channel", channel)
                    .setAttribute("maskedRecipient", OtpChallenges.maskRecipient(channel, recipient))
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
        if ((!"email".equals(channel) && !"sms".equals(channel)) || recipient == null) {
            challengeLoginError(context);
            return;
        }
        RealmModel realm = context.getRealm();
        KeycloakSession session = context.getSession();
        OtpChallenges.VerifyResult verifyResult = OtpChallenges.verify(
            session,
            realm,
            OtpChallenges.PURPOSE_LOGIN,
            channel,
            recipient,
            suppliedCode
        );
        if (verifyResult != OtpChallenges.VerifyResult.VERIFIED) {
            UserModel candidateUser = verifyResult == OtpChallenges.VerifyResult.WRONG_CODE
                ? OtpChallenges.findUserByChannel(session, realm, channel, recipient)
                : null;
            if (candidateUser != null) {
                context.getProtector().failedLogin(
                    realm,
                    candidateUser,
                    context.getConnection(),
                    session.getContext().getUri(),
                    java.util.Set.of("otp")
                );
            }
            challengeCodeError(context, channel, recipient);
            return;
        }
        UserModel user = OtpChallenges.findUserByChannel(session, realm, channel, recipient);
        if (user != null && !user.isEnabled()) {
            challengeLoginError(context);
            return;
        }
        if (user == null) {
            if (!realm.isRegistrationAllowed()) {
                challengeLoginError(context);
                return;
            }
            user = createVerifiedUser(session, realm, channel, recipient);
        } else if ("email".equals(channel)) {
            user.setEmailVerified(true);
        } else {
            user.setSingleAttribute("phone_number", recipient);
            user.setSingleAttribute("phone_number_verified", "true");
        }
        context.getProtector().successfulLogin(
            realm,
            user,
            context.getConnection(),
            session.getContext().getUri(),
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

    private UserModel findUserByIdentifier(KeycloakSession session, RealmModel realm, String identifier) {
        if (identifier.isBlank()) {
            return null;
        }
        String lowerIdentifier = identifier.toLowerCase();
        if (OtpChallenges.looksLikeEmail(lowerIdentifier)) {
            return session.users().getUserByEmail(realm, lowerIdentifier);
        }
        String normalizedPhone = OtpChallenges.normalizePhone(lowerIdentifier);
        if (normalizedPhone != null) {
            return OtpChallenges.findUserByChannel(session, realm, "sms", "+86" + normalizedPhone);
        }
        return session.users().getUserByUsername(realm, identifier);
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
                .setAttribute("maskedRecipient", OtpChallenges.maskRecipient(channel, recipient))
                .createForm("shared-code.ftl")
        );
    }

    private static String trim(String value) {
        return value == null ? "" : value.trim();
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
