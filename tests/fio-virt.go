// Assisted by Claude Opus
package test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type FioVirtResult struct {
	ClientStats []FioVirtJob `json:"client_stats"`
}

type FioVirtJob struct {
	Options map[string]string `json:"job options"`
	Read    FioVirtIO         `json:"read"`
	Write   FioVirtIO         `json:"write"`
}

type FioVirtIO struct {
	IOBytes int64          `json:"io_bytes"`
	BWBytes float64        `json:"bw_bytes"`
	IOPS    float64        `json:"iops"`
	ClatNS  FioVirtLatency `json:"clat_ns"`
}

type FioVirtLatency struct {
	Percentile map[string]float64 `json:"percentile"`
}

const (
	fioCacheDir = "/uat_scratchpad/fio-bin"
	fioPort     = "8765"
)

var (
	fioVersion   string
	fioBinary    string
	serverHost   string
	resultsDir   string
	readyTimeout time.Duration
)

func TestFioVirt(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "fio VM Storage Test Suite")
}

var _ = BeforeSuite(func() {
	fioVersion = os.Getenv("FIO_VERSION")
	Expect(fioVersion).NotTo(BeEmpty(), "FIO_VERSION must be set")

	serverHost = os.Getenv("SERVER_HOST")
	Expect(serverHost).NotTo(BeEmpty(), "SERVER_HOST must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	seconds, err := strconv.Atoi(os.Getenv("READY_TIMEOUT_SECONDS"))
	Expect(err).NotTo(HaveOccurred(), "READY_TIMEOUT_SECONDS must be an integer")
	readyTimeout = time.Duration(seconds) * time.Second

	fioBinary = filepath.Join(fioCacheDir, fioVersion, "fio")

	GinkgoWriter.Printf("fio version: %s\n", fioVersion)
	GinkgoWriter.Printf("Server: %s\n", serverHost)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
	GinkgoWriter.Printf("Ready timeout: %v\n", readyTimeout)
})

func run(dir string, name string, args ...string) {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	Expect(err).NotTo(HaveOccurred(), "%s %s failed: %s", name, strings.Join(args, " "), string(output))
}

// In client mode fio only sends a job file to the server, so everything after
// --client becomes one.
func splitClientCommand(args []string) ([]string, string) {
	var local []string
	var job strings.Builder
	remote := false
	for _, arg := range args {
		if !remote {
			local = append(local, arg)
			remote = strings.HasPrefix(arg, "--client=")
			continue
		}
		option := strings.TrimPrefix(arg, "--")
		if name, ok := strings.CutPrefix(option, "name="); ok {
			fmt.Fprintf(&job, "[%s]\n", name)
			continue
		}
		fmt.Fprintf(&job, "%s\n", option)
	}
	return local, job.String()
}

var _ = Describe("fio VM Storage", Ordered, func() {

	Describe("fio Build", Label("pass-fail"), func() {
		It("should have a fio binary built from the pinned version", func() {
			if _, err := os.Stat(fioBinary); err == nil {
				GinkgoWriter.Printf("Using cached %s\n", fioBinary)
				return
			}

			start := time.Now()
			src := "/tmp/fio-src"
			Expect(os.RemoveAll(src)).To(Succeed())
			run("/tmp", "git", "clone", "--depth", "1", "--branch", fioVersion,
				"https://github.com/axboe/fio.git", src)
			run(src, "./configure")
			run(src, "make", "-j16")

			Expect(os.MkdirAll(filepath.Dir(fioBinary), 0755)).To(Succeed())
			run(src, "cp", "fio", fioBinary+".tmp")
			Expect(os.Rename(fioBinary+".tmp", fioBinary)).To(Succeed())

			built := time.Since(start).Seconds()
			GinkgoWriter.Printf("Built %s in %.0fs\n", fioVersion, built)
			AddReportEntry("fio_build_seconds", built)
		})
	})

	Describe("VM Server Connectivity", Label("pass-fail"), func() {
		It("should become reachable on the fio server port", func() {
			addr := net.JoinHostPort(serverHost, fioPort)
			start := time.Now()
			deadline := start.Add(readyTimeout)

			var lastErr error
			for time.Now().Before(deadline) {
				conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
				if err == nil {
					conn.Close()
					waited := time.Since(start).Seconds()
					GinkgoWriter.Printf("fio server reachable after %.0fs\n", waited)
					AddReportEntry("server_ready_seconds", waited)
					return
				}
				lastErr = err
				time.Sleep(5 * time.Second)
			}
			Fail(fmt.Sprintf("cannot reach fio server at %s after %v: %v", addr, readyTimeout, lastErr))
		})
	})

	Describe("Storage Measurement", Label("pass-fail"), func() {
		It("should measure non-zero throughput", func() {
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			sweepCmdJSON := os.Getenv("SWEEP_COMMAND")
			Expect(sweepCmdJSON).NotTo(BeEmpty(), "SWEEP_COMMAND must be set")

			var sweepCmd []string
			Expect(json.Unmarshal([]byte(sweepCmdJSON), &sweepCmd)).To(Succeed(), "Failed to parse SWEEP_COMMAND")
			Expect(sweepCmd).NotTo(BeEmpty(), "SWEEP_COMMAND is empty")

			local, job := splitClientCommand(sweepCmd[1:])
			Expect(job).NotTo(BeEmpty(), "SWEEP_COMMAND has no job options after --client")
			jobFile := "/tmp/uat.fio"
			Expect(os.WriteFile(jobFile, []byte(job), 0644)).To(Succeed())
			GinkgoWriter.Printf("Job file:\n%s\n", job)

			cmd := exec.Command(fioBinary, append(local, jobFile)...)
			output, err := cmd.Output()
			GinkgoWriter.Printf("fio output:\n%s\n", string(output))
			Expect(err).NotTo(HaveOccurred(), "fio failed: %s", string(output))

			Expect(os.WriteFile(filepath.Join(resultsDir, "fio.json"), output, 0644)).To(Succeed())

			// The client prints a job summary line before the JSON document.
			start := bytes.Index(output, []byte("\n{"))
			Expect(start).To(BeNumerically(">=", 0), "no JSON in fio output")

			var result FioVirtResult
			Expect(json.Unmarshal(output[start:], &result)).To(Succeed())
			Expect(result.ClientStats).NotTo(BeEmpty(), "fio reported no client stats")

			stats := result.ClientStats[0]
			io := stats.Write
			if stats.Read.IOBytes > 0 {
				io = stats.Read
			}
			mbps := io.BWBytes / 1e6
			p50 := io.ClatNS.Percentile["50.000000"] / 1e6
			p99 := io.ClatNS.Percentile["99.000000"] / 1e6
			GinkgoWriter.Printf("%s bs=%s jobs=%s: %.1f MB/s, %.0f IOPS, p50 %.2f ms, p99 %.2f ms\n",
				stats.Options["rw"], stats.Options["bs"], stats.Options["numjobs"], mbps, io.IOPS, p50, p99)
			Expect(io.BWBytes).To(BeNumerically(">", 0), "measured throughput is zero")

			AddReportEntry("rw", stats.Options["rw"])
			AddReportEntry("block_size", stats.Options["bs"])
			AddReportEntry("jobs", stats.Options["numjobs"])
			AddReportEntry("throughput_mb_s", mbps)
			AddReportEntry("iops", io.IOPS)
			AddReportEntry("latency_p50_ms", p50)
			AddReportEntry("latency_p99_ms", p99)
		})
	})

})
