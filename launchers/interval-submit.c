/* FireSim interval ROI: whole retirement cycles containing these markers are
 * included. The child starts immediately before exec; the parent stops after
 * wait, before reporting status. No target device driver is required. */
#include <errno.h>
#include <string.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
  const char *status_path = NULL;
  int program = 1;
  if (argc > 1 && strcmp(argv[1], "--status-file") == 0) {
    if (argc < 4) {
      fprintf(stderr, "usage: interval-submit [--status-file PATH] <program> [args...]\n");
      return 2;
    }
    status_path = argv[2];
    program = 3;
  }
  if (argc <= program) {
    fprintf(stderr, "usage: interval-submit [--status-file PATH] <program> [args...]\n");
    return 2;
  }
  pid_t pid = fork();
  if (pid < 0) {
    perror("fork");
    return 1;
  }
  if (pid == 0) {
    asm volatile(".4byte 0x00008013" ::: "memory");
    execvp(argv[program], &argv[program]);
    perror("execvp");
    _exit(127);
  }
  int status;
  pid_t result;
  do { result = waitpid(pid, &status, 0); } while (result < 0 && errno == EINTR);
  asm volatile(".4byte 0x00010013" ::: "memory");
  if (result < 0) {
    perror("waitpid");
    return 1;
  }
  int code = WIFEXITED(status) ? WEXITSTATUS(status) :
             WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 1;
  // SPEC compares program stdout exactly. A separate status file avoids adding
  // launcher diagnostics to benchmark output; legacy invocations retain stdout.
  if (status_path) {
    FILE *status_file = fopen(status_path, "w");
    if (!status_file) { perror("status file"); return 1; }
    int wrote = fprintf(status_file, "%d\n", code);
    int closed = fclose(status_file);
    if (wrote < 0 || closed != 0) { perror("status file"); return 1; }
  } else {
    printf("interval-submit: exit_code=%d\n", code);
  }
  return code;
}
