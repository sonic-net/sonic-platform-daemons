//! Keep synchronous vendor reads away from the shutdown/sync path.

use std::sync::mpsc;
use std::time::Duration;

use platform_api::{PlatformApi, PlatformError, StorageDeviceInfo};
use pmon_common::cycles::{Cycles, Tick};
use tokio::sync::oneshot;

pub enum ReadError {
    Backend(PlatformError),
    Stopped(i32),
}

pub trait StorageSource {
    async fn read_storage(
        &mut self,
        cycles: &mut Cycles,
    ) -> Result<Vec<StorageDeviceInfo>, ReadError>;
    async fn next_cycle(&mut self, cycles: &mut Cycles, period: Duration) -> Tick;
}

// Direct handles keep the existing unit/parity seam. Production uses the
// worker below so it can finish the disk sync even while a vendor call blocks.
impl<P: PlatformApi + ?Sized> StorageSource for P {
    async fn read_storage(&mut self, _: &mut Cycles) -> Result<Vec<StorageDeviceInfo>, ReadError> {
        self.get_storage_devices().map_err(ReadError::Backend)
    }

    async fn next_cycle(&mut self, cycles: &mut Cycles, period: Duration) -> Tick {
        cycles.next(period, self).await
    }
}

enum Request {
    Read(oneshot::Sender<Result<Vec<StorageDeviceInfo>, PlatformError>>),
    Stop,
}

struct Control(mpsc::Sender<Request>);

impl PlatformApi for Control {
    fn notify_shutdown(&mut self) -> Result<(), PlatformError> {
        let _ = self.0.send(Request::Stop);
        Ok(())
    }
}

pub struct StorageWorker {
    control: Control,
    done: Option<oneshot::Receiver<()>>,
}

impl StorageWorker {
    pub fn new<P: PlatformApi + 'static>(mut platform: P) -> std::io::Result<Self> {
        let (sender, requests) = mpsc::channel();
        let (finished, done) = oneshot::channel();
        std::thread::Builder::new()
            .name("storage-reader".into())
            .spawn(move || {
                pmon_common::thread::abort_on_panic("storage reader", || {
                    while let Ok(Request::Read(answer)) = requests.recv() {
                        let _ = answer.send(platform.get_storage_devices());
                    }
                    pmon_common::cycles::notify_shutdown(&mut platform);
                    platform.finalize();
                    let _ = finished.send(());
                });
            })?;
        Ok(Self {
            control: Control(sender),
            done: Some(done),
        })
    }

    /// Finalize an idle worker normally, but never wait out an uninterruptible
    /// vendor read. The caller has already synced the last published counters;
    /// process exit will dispose of any worker still inside vendor code.
    pub async fn shutdown(&mut self) {
        let _ = self.control.notify_shutdown();
        if let Some(done) = self.done.take() {
            if tokio::time::timeout(Duration::from_secs(1), done)
                .await
                .is_err()
            {
                log::warn!("Storage worker is still blocked; counters are synced, leaving it to process exit");
            }
        }
    }
}

impl Drop for StorageWorker {
    fn drop(&mut self) {
        let _ = self.control.notify_shutdown();
    }
}

impl StorageSource for StorageWorker {
    async fn read_storage(
        &mut self,
        cycles: &mut Cycles,
    ) -> Result<Vec<StorageDeviceInfo>, ReadError> {
        let (answer, result) = oneshot::channel();
        self.control.0.send(Request::Read(answer)).map_err(|_| {
            ReadError::Backend(PlatformError::Backend("storage worker stopped".into()))
        })?;
        tokio::select! {
            biased;
            code = cycles.exit_requested() => Err(ReadError::Stopped(code)),
            result = result => result
                .map_err(|_| ReadError::Backend(PlatformError::Backend("storage worker stopped".into())))?
                .map_err(ReadError::Backend),
        }
    }

    async fn next_cycle(&mut self, cycles: &mut Cycles, period: Duration) -> Tick {
        cycles.next(period, &mut self.control).await
    }
}
