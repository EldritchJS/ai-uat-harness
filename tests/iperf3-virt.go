// Assisted by Claude Opus
package test

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"strconv"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type IperfVirtResult struct {
	Start IperfVirtStart `json:"start"`
	End   IperfVirtEnd   `json:"end"`
}

type IperfVirtStart struct {
	TestStart IperfVirtTestStart `json:"test_start"`
}

type IperfVirtTestStart struct {
	NumStreams int `json:"num_streams"`
	Reverse    int `json:"reverse"`
}

type IperfVirtEnd struct {
	SumReceived IperfVirtSum `json:"sum_received"`
}

type IperfVirtSum struct {
	BitsPerSecond float64 `json:"bits_per_second"`
}

var (
	serverHost   string
	resultsDir   string
	readyTimeout time.Duration
)

func TestIperf3Virt(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "iperf3 Pod to VM TCP Bandwidth Test Suite")
}

var _ = BeforeSuite(func() {
	serverHost = os.Getenv("SERVER_HOST")
	Expect(serverHost).NotTo(BeEmpty(), "SERVER_HOST must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	seconds, err := strconv.Atoi(os.Getenv("READY_TIMEOUT_SECONDS"))
	Expect(err).NotTo(HaveOccurred(), "READY_TIMEOUT_SECONDS must be an integer")
	readyTimeout = time.Duration(seconds) * time.Second

	GinkgoWriter.Printf("Server: %s\n", serverHost)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
	GinkgoWriter.Printf("Ready timeout: %v\n", readyTimeout)
})

var _ = Describe("iperf3 Pod to VM TCP Bandwidth", func() {

	Describe("VM Server Connectivity", Label("pass-fail"), func() {
		It("should become reachable on port 5201", func() {
			addr := fmt.Sprintf("%s:5201", serverHost)
			start := time.Now()
			deadline := start.Add(readyTimeout)

			var lastErr error
			for time.Now().Before(deadline) {
				conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
				if err == nil {
					conn.Close()
					waited := time.Since(start).Seconds()
					GinkgoWriter.Printf("iperf3 server reachable after %.0fs\n", waited)
					AddReportEntry("server_ready_seconds", waited)
					// Let the server finish with the probe connection.
					time.Sleep(2 * time.Second)
					return
				}
				lastErr = err
				time.Sleep(5 * time.Second)
			}
			Fail(fmt.Sprintf("cannot reach iperf3 server at %s after %v: %v", addr, readyTimeout, lastErr))
		})
	})

	Describe("Bandwidth Measurement", Label("pass-fail"), func() {
		It("should measure non-zero TCP bandwidth", func() {
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			sweepCmdJSON := os.Getenv("SWEEP_COMMAND")
			Expect(sweepCmdJSON).NotTo(BeEmpty(), "SWEEP_COMMAND must be set")

			var sweepCmd []string
			Expect(json.Unmarshal([]byte(sweepCmdJSON), &sweepCmd)).To(Succeed(), "Failed to parse SWEEP_COMMAND")
			Expect(sweepCmd).NotTo(BeEmpty(), "SWEEP_COMMAND is empty")

			cmd := exec.Command(sweepCmd[0], sweepCmd[1:]...)
			output, err := cmd.CombinedOutput()
			GinkgoWriter.Printf("iperf3 output:\n%s\n", string(output))
			Expect(err).NotTo(HaveOccurred(), "iperf3 failed: %s", string(output))

			var result IperfVirtResult
			Expect(json.Unmarshal(output, &result)).To(Succeed())

			streams := result.Start.TestStart.NumStreams
			direction := "pod-to-vm"
			if result.Start.TestStart.Reverse != 0 {
				direction = "vm-to-pod"
			}
			bps := result.End.SumReceived.BitsPerSecond
			gbps := bps / 1e9
			GinkgoWriter.Printf("TCP bandwidth %s with %d streams: %.2f Gbps\n", direction, streams, gbps)
			Expect(bps).To(BeNumerically(">", 0), "measured bandwidth is zero")

			AddReportEntry("direction", direction)
			AddReportEntry("streams", streams)
			AddReportEntry("bandwidth_gbps", gbps)
		})
	})

})
