# AGENTS.md

## 项目结构
- `app.py`：是项目文件


## 编码规范
- 不要写任何注释
- 同时支持macos和linux

## 测试命令
1. 新建一个beeware项目
2. 将你写好的app.py
```bash
yes '' | briefcase new && \
cp ./app.py ./helloworld && \
cd helloworld
briefcase dev
```


