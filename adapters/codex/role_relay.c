/* Static stdio relay for one character. Both paths are fixed by the launcher. */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <sys/file.h>

static int copy_chunk(int src, int dst) {
    char data[8192];
    ssize_t len;
    do {
        len = read(src, data, sizeof(data));
    } while (len < 0 && errno == EINTR);
    if (len <= 0)
        return (int)len;

    for (ssize_t offset = 0; offset < len;) {
        ssize_t n = write(dst, data + offset, (size_t)(len - offset));
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        offset += n;
    }
    return 1;
}

int main(void) {
    int requests = open("/channel/requests", O_WRONLY);
    int replies = open("/channel/replies", O_RDONLY);
    if (requests < 0 || replies < 0 || flock(replies, LOCK_EX | LOCK_NB) < 0)
        return 1;
    struct pollfd inputs[] = {{STDIN_FILENO, POLLIN, 0}, {replies, POLLIN, 0}};
    int outputs[] = {requests, STDOUT_FILENO};
    for (;;) {
        int ready = poll(inputs, 2, -1);
        if (ready < 0 && errno == EINTR)
            continue;
        if (ready < 0)
            return 1;
        for (int i = 0; i < 2; i++) {
            if (inputs[i].revents) {
                int result = copy_chunk(inputs[i].fd, outputs[i]);
                if (result <= 0)
                    return result < 0;
            }
        }
    }
}
