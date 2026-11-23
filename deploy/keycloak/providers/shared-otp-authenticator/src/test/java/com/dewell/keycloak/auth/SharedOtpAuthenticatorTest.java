package com.dewell.keycloak.auth;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.RETURNS_SELF;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockStatic;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import jakarta.ws.rs.core.MultivaluedHashMap;
import jakarta.ws.rs.core.Response;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.http.HttpRequest;
import org.keycloak.models.ClientModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.SingleUseObjectProvider;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;
import org.keycloak.protocol.LoginProtocol;
import org.keycloak.protocol.oidc.OIDCLoginProtocol;
import org.keycloak.sessions.AuthenticationSessionModel;
import org.mockito.MockedStatic;

final class SharedOtpAuthenticatorTest {
    private static final String PHONE_RECIPIENT = "+8613800000000";
    private static final String EMAIL_RECIPIENT = "user@example.com";
    private static final String CURRENT_CHALLENGE_KEY = "otp:current";
    private static final String OLD_CHALLENGE_ID = "challenge-old";
    private static final String NEW_CHALLENGE_ID = "challenge-new";
    private static final String OLD_CHALLENGE_DATA_KEY = "otp:data:" + OLD_CHALLENGE_ID;
    private static final String NEW_CHALLENGE_DATA_KEY = "otp:data:" + NEW_CHALLENGE_ID;

    @Test
    void staleVerificationCannotConsumeAReplacementChallenge() {
        SingleUseObjectProvider store = mock(SingleUseObjectProvider.class);
        AtomicReference<Map<String, String>> currentChallenge = new AtomicReference<>(
            Map.of("challenge_id", OLD_CHALLENGE_ID)
        );
        when(store.get(CURRENT_CHALLENGE_KEY)).thenAnswer(invocation -> currentChallenge.get());
        when(store.remove(OLD_CHALLENGE_DATA_KEY)).thenAnswer(invocation -> {
            currentChallenge.set(Map.of("challenge_id", NEW_CHALLENGE_ID));
            return Map.of("challenge_id", OLD_CHALLENGE_ID);
        });

        boolean consumedCurrentChallenge = OtpChallenges.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );

        assertFalse(consumedCurrentChallenge);
        verify(store).remove(OLD_CHALLENGE_DATA_KEY);
        verify(store, never()).remove(NEW_CHALLENGE_DATA_KEY);
    }

    @Test
    void challengeCanOnlyBeConsumedOnce() {
        SingleUseObjectProvider store = mock(SingleUseObjectProvider.class);
        when(store.remove(OLD_CHALLENGE_DATA_KEY)).thenReturn(
            Map.of("challenge_id", OLD_CHALLENGE_ID),
            null
        );
        when(store.get(CURRENT_CHALLENGE_KEY)).thenReturn(
            Map.of("challenge_id", OLD_CHALLENGE_ID)
        );

        boolean firstConsumption = OtpChallenges.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );
        boolean replayedConsumption = OtpChallenges.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );

        assertTrue(firstConsumption);
        assertFalse(replayedConsumption);
    }

    @Test
    void phoneUserIsNamedByE164Number() {
        KeycloakSession session = mock(KeycloakSession.class);
        UserProvider users = mock(UserProvider.class);
        RealmModel realm = mock(RealmModel.class);
        UserModel user = mock(UserModel.class);
        when(session.users()).thenReturn(users);
        when(users.addUser(realm, PHONE_RECIPIENT)).thenReturn(user);

        UserModel createdUser = SharedOtpAuthenticator.createVerifiedUser(
            session,
            realm,
            "sms",
            PHONE_RECIPIENT
        );

        assertSame(user, createdUser);
        verify(user).setSingleAttribute("phone_number", PHONE_RECIPIENT);
        verify(user).setSingleAttribute("phone_number_verified", "true");
    }

    @Test
    void emailCodeOnlyMatchesVerifiedEmail() {
        KeycloakSession session = mock(KeycloakSession.class);
        UserProvider users = mock(UserProvider.class);
        RealmModel realm = mock(RealmModel.class);
        UserModel unverifiedUser = mock(UserModel.class);
        UserModel verifiedUser = mock(UserModel.class);
        when(session.users()).thenReturn(users);
        when(unverifiedUser.isEmailVerified()).thenReturn(false);
        when(verifiedUser.isEmailVerified()).thenReturn(true);

        when(users.getUserByEmail(realm, EMAIL_RECIPIENT)).thenReturn(unverifiedUser);
        assertNull(OtpChallenges.findUserByChannel(session, realm, "email", EMAIL_RECIPIENT));

        when(users.getUserByEmail(realm, EMAIL_RECIPIENT)).thenReturn(verifiedUser);
        assertSame(
            verifiedUser,
            OtpChallenges.findUserByChannel(session, realm, "email", EMAIL_RECIPIENT)
        );
    }

    @ParameterizedTest
    @ValueSource(strings = {"shared", "freshai"})
    void verifiedCodeForOldEmailUsernameReturnsToLoginWithoutCreatingAnAccount(String loginTheme) {
        AuthenticationFlowContext context = mock(AuthenticationFlowContext.class);
        AuthenticationSessionModel authSession = mock(AuthenticationSessionModel.class);
        KeycloakSession session = mock(KeycloakSession.class);
        UserProvider users = mock(UserProvider.class);
        RealmModel realm = mock(RealmModel.class);
        LoginFormsProvider loginForm = mock(LoginFormsProvider.class);
        HttpRequest request = mock(HttpRequest.class);
        UserModel existingUser = mock(UserModel.class);
        ClientModel client = mock(ClientModel.class);
        LoginProtocol protocol = mock(LoginProtocol.class, RETURNS_SELF);
        Response redirectResponse = mock(Response.class);
        MultivaluedHashMap<String, String> parameters = new MultivaluedHashMap<>();
        parameters.putSingle("method", "email");
        parameters.putSingle("step", "verify");
        parameters.putSingle("code", "123456");
        when(request.getDecodedFormParameters()).thenReturn(parameters);
        when(context.getHttpRequest()).thenReturn(request);
        when(context.getAuthenticationSession()).thenReturn(authSession);
        when(context.getSession()).thenReturn(session);
        when(context.getRealm()).thenReturn(realm);
        when(authSession.getProtocol()).thenReturn(OIDCLoginProtocol.LOGIN_PROTOCOL);
        when(authSession.getClient()).thenReturn(client);
        when(client.getAttribute("login_theme")).thenReturn(loginTheme);
        if ("freshai".equals(loginTheme)) {
            when(session.getProvider(LoginProtocol.class, OIDCLoginProtocol.LOGIN_PROTOCOL)).thenReturn(protocol);
            when(protocol.sendError(
                authSession, LoginProtocol.Error.CONSENT_DENIED, "freshai_email_login_unavailable"
            )).thenReturn(redirectResponse);
        } else {
            when(context.form()).thenReturn(loginForm);
            when(loginForm.setError("sharedCodeSignInUnavailable")).thenReturn(loginForm);
        }
        when(authSession.getAuthNote("shared_otp_channel")).thenReturn("email");
        when(authSession.getAuthNote("shared_otp_recipient")).thenReturn(EMAIL_RECIPIENT);
        when(realm.isRegistrationAllowed()).thenReturn(true);
        when(session.users()).thenReturn(users);
        when(users.getUserByUsername(realm, EMAIL_RECIPIENT)).thenReturn(existingUser);

        try (MockedStatic<OtpChallenges> otpChallenges = mockStatic(OtpChallenges.class)) {
            otpChallenges.when(() -> OtpChallenges.verify(
                session, realm, OtpChallenges.PURPOSE_LOGIN, "email", EMAIL_RECIPIENT, "123456"
            )).thenReturn(OtpChallenges.VerifyResult.VERIFIED);
            otpChallenges.when(() -> OtpChallenges.findUserByChannel(
                session, realm, "email", EMAIL_RECIPIENT
            )).thenCallRealMethod();
            new SharedOtpAuthenticator().action(context);
        }

        if ("freshai".equals(loginTheme)) {
            verify(context).forceChallenge(redirectResponse);
            verify(context, never()).form();
        } else {
            verify(loginForm).createForm("shared-login.ftl");
            verify(context, never()).forceChallenge(any());
        }
        verify(authSession).removeAuthNote("shared_otp_recipient");
        verify(users, never()).addUser(realm, EMAIL_RECIPIENT);
        verify(context, never()).setUser(any());
        verify(context, never()).success();
    }
}
