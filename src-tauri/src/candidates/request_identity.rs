//! Pin source and request generation before waiting for the shared CLI slot.
use crate::coreimage::{self, CoreImageError};
use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex, OnceLock,
    },
    time::SystemTime,
};
static NEXT: AtomicU64 = AtomicU64::new(0);
static CURRENT: OnceLock<Mutex<HashMap<PathBuf, u64>>> = OnceLock::new();
fn current() -> &'static Mutex<HashMap<PathBuf, u64>> {
    CURRENT.get_or_init(|| Mutex::new(HashMap::new()))
}
fn stale() -> CoreImageError {
    CoreImageError::new(
        "CANDIDATES_STALE",
        "現像元または候補要求が更新されたため旧結果を破棄しました",
    )
}
#[derive(PartialEq, Eq)]
struct Stamp {
    len: u64,
    modified: Option<SystemTime>,
    ino: u64,
}
impl Stamp {
    fn read(path: &Path) -> Result<Self, CoreImageError> {
        use std::os::unix::fs::MetadataExt;
        let m = fs::metadata(path).map_err(|_| stale())?;
        Ok(Self {
            len: m.len(),
            modified: m.modified().ok(),
            ino: m.ino(),
        })
    }
}
pub(super) struct RequestIdentity {
    data: PathBuf,
    id: u64,
    pub root: PathBuf,
    pub input: PathBuf,
    pub sidecar: PathBuf,
    config_stamp: Stamp,
    input_stamp: Stamp,
    sidecar_stamp: Stamp,
}
impl RequestIdentity {
    pub fn start(data: &Path, sidecar: &Path, capture: &str) -> Result<Self, CoreImageError> {
        let config_stamp = Stamp::read(&data.join("config.json")).map_err(|_| {
            coreimage::read_source_root(data)
                .err()
                .unwrap_or_else(stale)
        })?;
        let root = coreimage::read_source_root(data)?;
        let input = coreimage::resolve_arw(&root, capture)?;
        let sidecar = sidecar.canonicalize().map_err(|_| stale())?;
        let input_stamp = Stamp::read(&input)?;
        let sidecar_stamp = Stamp::read(&sidecar)?;
        if config_stamp != Stamp::read(&data.join("config.json"))? {
            return Err(stale());
        }
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        current()
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .insert(data.to_path_buf(), id);
        Ok(Self {
            data: data.to_path_buf(),
            id,
            root,
            input,
            sidecar,
            config_stamp,
            input_stamp,
            sidecar_stamp,
        })
    }
    pub fn ensure_current(&self) -> Result<(), CoreImageError> {
        let current_id = current()
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .get(&self.data)
            .copied();
        if current_id != Some(self.id)
            || self.config_stamp != Stamp::read(&self.data.join("config.json"))?
            || self.input_stamp != Stamp::read(&self.input)?
            || self.sidecar_stamp != Stamp::read(&self.sidecar)?
        {
            return Err(stale());
        }
        Ok(())
    }
}
impl Drop for RequestIdentity {
    fn drop(&mut self) {
        let mut map = current().lock().unwrap_or_else(|p| p.into_inner());
        if map.get(&self.data) == Some(&self.id) {
            map.remove(&self.data);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    static TEST_ID: AtomicU64 = AtomicU64::new(0);
    #[test]
    fn stale_source_replacement_switch_back_and_new_request_are_discarded() {
        let dir = std::env::temp_dir().join(format!(
            "geoframe-roi-identity-{}-{}",
            std::process::id(),
            TEST_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(dir.join("raw")).unwrap();
        fs::write(dir.join("raw/DSC07402.ARW"), "test").unwrap();
        fs::write(dir.join("cli"), "cli").unwrap();
        let config = json_config(&dir.join("raw"));
        fs::write(dir.join("config.json"), &config).unwrap();
        let first = RequestIdentity::start(&dir, &dir.join("cli"), "DSC07402").unwrap();
        first.ensure_current().unwrap();
        let second = RequestIdentity::start(&dir, &dir.join("cli"), "DSC07402").unwrap();
        assert_eq!(first.ensure_current().unwrap_err().code, "CANDIDATES_STALE");
        drop(first);
        second.ensure_current().unwrap();
        // Even a return to the same folder is a new config identity (atomic replacement).
        fs::write(dir.join("replacement"), &config).unwrap();
        fs::rename(dir.join("replacement"), dir.join("config.json")).unwrap();
        assert!(second.ensure_current().is_err());
        drop(second);
        let third = RequestIdentity::start(&dir, &dir.join("cli"), "DSC07402").unwrap();
        fs::write(dir.join("raw/replacement"), "test").unwrap();
        fs::rename(dir.join("raw/replacement"), dir.join("raw/DSC07402.ARW")).unwrap();
        assert!(third.ensure_current().is_err());
        drop(third);
        fs::remove_dir_all(dir).unwrap();
    }
    fn json_config(root: &Path) -> String {
        serde_json::json!({"source_root":root}).to_string()
    }
}
