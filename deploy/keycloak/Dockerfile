ARG KC_VERSION=26.8.2

FROM maven:3.9-eclipse-temurin-21 AS provider-build
WORKDIR /workspace
COPY providers/shared-otp-authenticator/pom.xml ./pom.xml
COPY providers/shared-otp-authenticator/src ./src
RUN mvn -B test package

FROM quay.io/keycloak/keycloak:${KC_VERSION} AS keycloak-build
COPY --from=provider-build /workspace/target/shared-otp-authenticator-1.0.0.jar /opt/keycloak/providers/shared-otp-authenticator.jar
COPY providers/shared-otp-authenticator/src/main/resources/theme/shared /opt/keycloak/themes/shared
COPY providers/shared-otp-authenticator/src/main/resources/theme/freshai /opt/keycloak/themes/freshai
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:${KC_VERSION}
COPY --from=keycloak-build /opt/keycloak/ /opt/keycloak/
