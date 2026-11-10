package com.dewell.keycloak.auth;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.SingleUseObjectProvider;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;

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

        boolean consumedCurrentChallenge = SharedOtpAuthenticator.consumeChallengeIfCurrent(
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

        boolean firstConsumption = SharedOtpAuthenticator.consumeChallengeIfCurrent(
            store,
            CURRENT_CHALLENGE_KEY,
            OLD_CHALLENGE_DATA_KEY,
            OLD_CHALLENGE_ID
        );
        boolean replayedConsumption = SharedOtpAuthenticator.consumeChallengeIfCurrent(
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
        assertNull(SharedOtpAuthenticator.findUserByChannel(session, realm, "email", EMAIL_RECIPIENT));

        when(users.getUserByEmail(realm, EMAIL_RECIPIENT)).thenReturn(verifiedUser);
        assertSame(
            verifiedUser,
            SharedOtpAuthenticator.findUserByChannel(session, realm, "email", EMAIL_RECIPIENT)
        );
    }
}
