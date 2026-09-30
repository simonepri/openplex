// Derives opaque principal identifiers from OIDC issuer and subject claims for snapshot ownership.

package main

import (
	"crypto/sha256"
	"encoding/base64"
)

func snapshotPrincipalID(issuer, subject string) string {
	digest := sha256.Sum256([]byte(issuer + "\x00" + subject))
	return base64.RawURLEncoding.EncodeToString(digest[:])
}
