package com.dewell.keycloak.auth;

import jakarta.ws.rs.core.MultivaluedMap;
import org.keycloak.authentication.InitiatedActionSupport;
import org.keycloak.authentication.RequiredActionContext;
import org.keycloak.authentication.RequiredActionProvider;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * Application-initiated action that binds or replaces a verified email or phone number.
 *
 * <p>The new contact is written only after the user proves ownership with a code; a
 * contact held by another account is rejected. Keycloak re-authenticates the user first
 * when the last sign-in is older than the default maximum authentication age.
 */
final class SharedBindContactAction implements RequiredActionProvider {
    private static final String AUTH_NOTE_RECIPIENT = "shared_bind_recipient";

    private final String channel;

    SharedBindContactAction(String channel) {
        this.channel = channel;
    }

    @Override
    public InitiatedActionSupport initiatedActionSupport() {
        return InitiatedActionSupport.SUPPORTED;
    }

    @Override
    public void evaluateTriggers(RequiredActionContext context) {
    }

    @Override
    public void requiredActionChallenge(RequiredActionContext context) {
        context.challenge(contactForm(context).createForm("shared-bind.ftl"));
    }

    @Override
    public void processAction(RequiredActionContext context) {
        MultivaluedMap<String, String> form = context.getHttpRequest().getDecodedFormParameters();
        String step = form.getFirst("step");
        if ("verify".equals(step)) {
            verifyCode(context, form.getFirst("code"));
            return;
        }
        requestCode(context, form.getFirst("identifier"));
    }

    private void requestCode(RequiredActionContext context, String rawRecipient) {
        KeycloakSession session = context.getSession();
        RealmModel realm = context.getRealm();
        UserModel user = context.getUser();
        String recipient = OtpChallenges.normalizeRecipient(channel, rawRecipient);
        if (recipient == null && (rawRecipient == null || rawRecipient.isBlank())) {
            recipient = context.getAuthenticationSession().getAuthNote(AUTH_NOTE_RECIPIENT);
        }
        if (recipient == null) {
            context.challenge(contactForm(context).setError("sharedBindInvalid").createForm("shared-bind.ftl"));
            return;
        }
        if (recipient.equals(currentContact(user))) {
            context.challenge(contactForm(context).setError("sharedBindSame").createForm("shared-bind.ftl"));
            return;
        }
        if (OtpChallenges.heldByAnotherUser(session, realm, channel, recipient, user)) {
            context.challenge(contactForm(context).setError("sharedBindInUse").createForm("shared-bind.ftl"));
            return;
        }
        if (!OtpChallenges.allowRequest(session, realm, context.getConnection().getRemoteAddr())) {
            context.challenge(contactForm(context).setError("sharedBindFailed").createForm("shared-bind.ftl"));
            return;
        }
        try {
            OtpChallenges.IssueResult issueResult = OtpChallenges.issue(
                session,
                realm,
                OtpChallenges.PURPOSE_BIND,
                channel,
                recipient
            );
            if (issueResult == OtpChallenges.IssueResult.COOLING_DOWN) {
                context.challenge(contactForm(context).setError("sharedBindCoolingDown").createForm("shared-bind.ftl"));
                return;
            }
        } catch (Exception exception) {
            context.challenge(contactForm(context).setError("sharedBindFailed").createForm("shared-bind.ftl"));
            return;
        }
        context.getAuthenticationSession().setAuthNote(AUTH_NOTE_RECIPIENT, recipient);
        context.challenge(codeForm(context, recipient).createForm("shared-code.ftl"));
    }

    private void verifyCode(RequiredActionContext context, String suppliedCode) {
        AuthenticationSessionModel authSession = context.getAuthenticationSession();
        String recipient = authSession.getAuthNote(AUTH_NOTE_RECIPIENT);
        if (recipient == null) {
            context.challenge(contactForm(context).setError("sharedBindFailed").createForm("shared-bind.ftl"));
            return;
        }
        KeycloakSession session = context.getSession();
        RealmModel realm = context.getRealm();
        OtpChallenges.VerifyResult verifyResult = OtpChallenges.verify(
            session,
            realm,
            OtpChallenges.PURPOSE_BIND,
            channel,
            recipient,
            suppliedCode
        );
        if (verifyResult != OtpChallenges.VerifyResult.VERIFIED) {
            context.challenge(codeForm(context, recipient).setError("sharedCodeInvalid").createForm("shared-code.ftl"));
            return;
        }
        UserModel user = context.getUser();
        // 发码与验证之间可能已被其他账号占用，写入前再查一次。
        if (OtpChallenges.heldByAnotherUser(session, realm, channel, recipient, user)) {
            authSession.removeAuthNote(AUTH_NOTE_RECIPIENT);
            context.challenge(contactForm(context).setError("sharedBindInUse").createForm("shared-bind.ftl"));
            return;
        }
        applyContact(user, channel, recipient);
        authSession.removeAuthNote(AUTH_NOTE_RECIPIENT);
        context.getEvent().detail("bound_channel", channel);
        context.success();
    }

    static void applyContact(UserModel user, String channel, String recipient) {
        if ("email".equals(channel)) {
            user.setEmail(recipient);
            user.setEmailVerified(true);
            return;
        }
        user.setSingleAttribute("phone_number", recipient);
        user.setSingleAttribute("phone_number_verified", "true");
    }

    private String currentContact(UserModel user) {
        if ("email".equals(channel)) {
            return user.getEmail();
        }
        return user.getFirstAttribute("phone_number");
    }

    private LoginFormsProvider contactForm(RequiredActionContext context) {
        String current = currentContact(context.getUser());
        LoginFormsProvider form = context.form().setAttribute("channel", channel);
        if (current != null && !current.isBlank()) {
            form.setAttribute("maskedCurrent", OtpChallenges.maskRecipient(channel, current));
        }
        return form;
    }

    private LoginFormsProvider codeForm(RequiredActionContext context, String recipient) {
        return context.form()
            .setAttribute("channel", channel)
            .setAttribute("bindMode", true)
            .setAttribute("maskedRecipient", OtpChallenges.maskRecipient(channel, recipient));
    }

    @Override
    public void close() {
    }
}
