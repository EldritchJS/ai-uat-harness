// Assisted by Claude Opus
package test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
)

type FioResult struct {
	Jobs []FioJob `json:"jobs"`
}

type FioJob struct {
	Options map[string]string `json:"job options"`
	Read    FioIO             `json:"read"`
	Write   FioIO             `json:"write"`
}

type FioIO struct {
	IOBytes int64      `json:"io_bytes"`
	BWBytes float64    `json:"bw_bytes"`
	IOPS    float64    `json:"iops"`
	ClatNS  FioLatency `json:"clat_ns"`
}

type FioLatency struct {
	Percentile map[string]float64 `json:"percentile"`
}

const fioCacheDir = "/uat_scratchpad/fio-bin"

var (
	fioVersion string
	fioBinary  string
	resultsDir string
)

func TestFio(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "fio Storage Test Suite")
}

var _ = BeforeSuite(func() {
	fioVersion = os.Getenv("FIO_VERSION")
	Expect(fioVersion).NotTo(BeEmpty(), "FIO_VERSION must be set")

	resultsDir = os.Getenv("RESULTS_DIR")
	Expect(resultsDir).NotTo(BeEmpty(), "RESULTS_DIR must be set")

	fioBinary = filepath.Join(fioCacheDir, fioVersion, "fio")

	GinkgoWriter.Printf("fio version: %s\n", fioVersion)
	GinkgoWriter.Printf("Results: %s\n", resultsDir)
})

func run(dir string, name string, args ...string) {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	output, err := cmd.CombinedOutput()
	Expect(err).NotTo(HaveOccurred(), "%s %s failed: %s", name, strings.Join(args, " "), string(output))
}

var _ = Describe("fio Storage", Ordered, func() {

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

	Describe("Storage Measurement", Label("pass-fail"), func() {
		It("should measure non-zero throughput", func() {
			Expect(os.MkdirAll(resultsDir, 0755)).To(Succeed())

			sweepCmdJSON := os.Getenv("SWEEP_COMMAND")
			Expect(sweepCmdJSON).NotTo(BeEmpty(), "SWEEP_COMMAND must be set")

			var sweepCmd []string
			Expect(json.Unmarshal([]byte(sweepCmdJSON), &sweepCmd)).To(Succeed(), "Failed to parse SWEEP_COMMAND")
			Expect(sweepCmd).NotTo(BeEmpty(), "SWEEP_COMMAND is empty")

			for _, arg := range sweepCmd {
				if dir, ok := strings.CutPrefix(arg, "--directory="); ok {
					Expect(os.MkdirAll(dir, 0755)).To(Succeed())
				}
			}

			cmd := exec.Command(fioBinary, sweepCmd[1:]...)
			output, err := cmd.Output()
			GinkgoWriter.Printf("fio output:\n%s\n", string(output))
			Expect(err).NotTo(HaveOccurred(), "fio failed: %s", string(output))

			Expect(os.WriteFile(filepath.Join(resultsDir, "fio.json"), output, 0644)).To(Succeed())

			var result FioResult
			Expect(json.Unmarshal(output, &result)).To(Succeed())
			Expect(result.Jobs).NotTo(BeEmpty(), "fio reported no jobs")

			job := result.Jobs[0]
			io := job.Write
			if job.Read.IOBytes > 0 {
				io = job.Read
			}
			mbps := io.BWBytes / 1e6
			p50 := io.ClatNS.Percentile["50.000000"] / 1e6
			p99 := io.ClatNS.Percentile["99.000000"] / 1e6
			GinkgoWriter.Printf("%s bs=%s jobs=%s: %.1f MB/s, %.0f IOPS, p50 %.2f ms, p99 %.2f ms\n",
				job.Options["rw"], job.Options["bs"], job.Options["numjobs"], mbps, io.IOPS, p50, p99)
			Expect(io.BWBytes).To(BeNumerically(">", 0), "measured throughput is zero")

			AddReportEntry("rw", job.Options["rw"])
			AddReportEntry("block_size", job.Options["bs"])
			AddReportEntry("jobs", job.Options["numjobs"])
			AddReportEntry("throughput_mb_s", mbps)
			AddReportEntry("iops", io.IOPS)
			AddReportEntry("latency_p50_ms", p50)
			AddReportEntry("latency_p99_ms", p99)
		})
	})

})
