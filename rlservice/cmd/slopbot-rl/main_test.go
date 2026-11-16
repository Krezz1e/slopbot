package main

import "testing"

func TestValidateLoopbackAddress(t *testing.T) {
	for _, address := range []string{"127.0.0.1:8765", "[::1]:8765", "localhost:8765"} {
		if err := validateLoopbackAddress(address); err != nil {
			t.Errorf("expected %s to be accepted: %v", address, err)
		}
	}
	for _, address := range []string{"0.0.0.0:8765", "192.168.1.10:8765", ":8765"} {
		if err := validateLoopbackAddress(address); err == nil {
			t.Errorf("expected %s to be rejected", address)
		}
	}
}
